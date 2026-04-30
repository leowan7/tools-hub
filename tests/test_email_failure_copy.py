"""Failure-path copy in the job-completion email.

Locks the copy that customers see when a tool run fails:

* Subject line says "failed" (not "ready").
* HTML headline + plain-text headline say "failed" (not "is ready").
* CTA is the muted/grey "View job details" button (not the green "View results").
* Summary mentions the credit refund when the pipeline produced no work.

Email *delivery* is not exercised here — only rendering.
"""

from __future__ import annotations

import uuid

from shared import email as email_mod
from shared.jobs import ToolJob


def _job(**over) -> ToolJob:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "boltzgen",
        "preset": "pilot",
        "status": "failed",
        "inputs": {},
        "result": None,
        "error": {"bucket": "pipeline", "detail": "run_pipeline exited 1 (CIF parse)"},
        "credits_cost": 10,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": None,
        "created_at": "2026-04-30T12:00:00Z",
        "started_at": None,
        "completed_at": "2026-04-30T12:00:05Z",
    }
    base.update(over)
    return ToolJob.from_row(base)


class TestFailureHeadline:
    def test_html_says_failed_not_ready(self):
        html = email_mod._render_html(
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", tone="failed"
        )
        assert "BoltzGen run failed" in html
        assert "is ready" not in html

    def test_text_says_failed_not_ready(self):
        text = email_mod._render_text(
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", tone="failed"
        )
        assert "BoltzGen run failed" in text
        assert "is ready" not in text

    def test_success_still_says_ready(self):
        html = email_mod._render_html(
            job=_job(status="succeeded", error=None),
            job_url="https://tools.ranomics.com/jobs/x",
            tone="success",
        )
        assert "is ready" in html
        assert "run failed" not in html


class TestFailureCTA:
    def test_html_cta_is_muted_view_details(self):
        html = email_mod._render_html(
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", tone="failed"
        )
        assert "View job details" in html
        # Muted grey, not the success-green
        assert "background:#525252" in html
        assert "background:#1f9d55" not in html

    def test_text_cta_is_view_details(self):
        text = email_mod._render_text(
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", tone="failed"
        )
        assert "View job details" in text
        assert "View results" not in text


class TestFailureSummaryMentionsRefund:
    def test_no_gpu_time_summary_mentions_refund(self):
        """When complete_job will full-refund, the summary must say so."""
        summary = email_mod._result_summary(_job(credits_cost=10), tone="failed")
        assert "10 credits were refunded" in summary
        assert "run_pipeline exited 1" in summary

    def test_singular_credit_grammar(self):
        summary = email_mod._result_summary(_job(credits_cost=1), tone="failed")
        assert "1 credit was refunded" in summary

    def test_real_gpu_time_summary_skips_refund_claim(self):
        """When GPU time was consumed (no refund), don't claim one happened."""
        summary = email_mod._result_summary(
            _job(gpu_seconds_used=420), tone="failed"
        )
        assert "refunded" not in summary
        assert "did not complete" in summary

    def test_zero_credit_cost_skips_refund_claim(self):
        summary = email_mod._result_summary(
            _job(credits_cost=0), tone="failed"
        )
        assert "refunded" not in summary


# ---------------------------------------------------------------------------
# Empty-tone (succeeded with 0 candidates / 0 sequences) email copy
# ---------------------------------------------------------------------------


class TestResultTone:
    """``_result_tone`` derives the email tone from job state."""

    def test_failed_status_is_failed_tone(self):
        assert email_mod._result_tone(_job(status="failed")) == "failed"

    def test_succeeded_with_zero_candidates_is_empty(self):
        job = _job(status="succeeded", error=None, result={"candidates": []})
        assert email_mod._result_tone(job) == "empty"

    def test_succeeded_with_zero_sequences_is_empty(self):
        job = _job(status="succeeded", error=None, result={"sequences": []},
                   tool="mpnn")
        assert email_mod._result_tone(job) == "empty"

    def test_succeeded_with_candidates_is_success(self):
        job = _job(status="succeeded", error=None,
                   result={"candidates": [{"rank": 1, "scores": {}}]})
        assert email_mod._result_tone(job) == "success"

    def test_succeeded_with_pdb_b64_is_success(self):
        job = _job(status="succeeded", error=None,
                   result={"pdb_b64": "abc", "mean_plddt": 87.5},
                   tool="af2")
        assert email_mod._result_tone(job) == "success"

    def test_succeeded_with_unknown_shape_is_success(self):
        """Forward-compat: unknown shapes default to success — never empty."""
        job = _job(status="succeeded", error=None, result={"future_field": "x"})
        assert email_mod._result_tone(job) == "success"


class TestEmptyToneRendering:
    """The 'finished but no candidates' case is the soft-fail UX class."""

    def _empty_job(self, **over):
        base = {"status": "succeeded", "error": None,
                "result": {"candidates": []}, "tool": "rfdiffusion"}
        base.update(over)
        return _job(**base)

    def test_html_headline_says_no_candidates_not_ready(self):
        html = email_mod._render_html(
            job=self._empty_job(),
            job_url="https://tools.ranomics.com/jobs/x",
            tone="empty",
        )
        assert "no candidates" in html
        assert "is ready" not in html

    def test_html_cta_is_muted_view_details(self):
        html = email_mod._render_html(
            job=self._empty_job(),
            job_url="https://tools.ranomics.com/jobs/x",
            tone="empty",
        )
        assert "View job details" in html
        assert "background:#525252" in html
        assert "background:#1f9d55" not in html

    def test_text_headline_says_no_candidates(self):
        text = email_mod._render_text(
            job=self._empty_job(),
            job_url="https://tools.ranomics.com/jobs/x",
            tone="empty",
        )
        assert "no candidates" in text
        assert "is ready" not in text

    def test_summary_explains_why_and_suggests_remediation(self):
        summary = email_mod._result_summary(self._empty_job(), tone="empty")
        assert "no passing candidates" in summary
        # Steers user toward an actionable remedy
        assert "binder length" in summary or "number of designs" in summary

    def test_summary_for_empty_sequence_run(self):
        job = self._empty_job(result={"sequences": []}, tool="mpnn")
        summary = email_mod._result_summary(job, tone="empty")
        assert "no sequences" in summary
