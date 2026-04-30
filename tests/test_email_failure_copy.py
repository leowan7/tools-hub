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
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", success=False
        )
        assert "BoltzGen run failed" in html
        assert "is ready" not in html

    def test_text_says_failed_not_ready(self):
        text = email_mod._render_text(
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", success=False
        )
        assert "BoltzGen run failed" in text
        assert "is ready" not in text

    def test_success_still_says_ready(self):
        html = email_mod._render_html(
            job=_job(status="succeeded", error=None),
            job_url="https://tools.ranomics.com/jobs/x",
            success=True,
        )
        assert "is ready" in html
        assert "run failed" not in html


class TestFailureCTA:
    def test_html_cta_is_muted_view_details(self):
        html = email_mod._render_html(
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", success=False
        )
        assert "View job details" in html
        # Muted grey, not the success-green
        assert "background:#525252" in html
        assert "background:#1f9d55" not in html

    def test_text_cta_is_view_details(self):
        text = email_mod._render_text(
            job=_job(), job_url="https://tools.ranomics.com/jobs/x", success=False
        )
        assert "View job details" in text
        assert "View results" not in text


class TestFailureSummaryMentionsRefund:
    def test_no_gpu_time_summary_mentions_refund(self):
        """When complete_job will full-refund, the summary must say so."""
        summary = email_mod._result_summary(_job(credits_cost=10), success=False)
        assert "10 credits were refunded" in summary
        assert "run_pipeline exited 1" in summary

    def test_singular_credit_grammar(self):
        summary = email_mod._result_summary(_job(credits_cost=1), success=False)
        assert "1 credit was refunded" in summary

    def test_real_gpu_time_summary_skips_refund_claim(self):
        """When GPU time was consumed (no refund), don't claim one happened."""
        summary = email_mod._result_summary(
            _job(gpu_seconds_used=420), success=False
        )
        assert "refunded" not in summary
        assert "did not complete" in summary

    def test_zero_credit_cost_skips_refund_claim(self):
        summary = email_mod._result_summary(
            _job(credits_cost=0), success=False
        )
        assert "refunded" not in summary
