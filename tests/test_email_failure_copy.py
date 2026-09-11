"""Failure-path copy in the job-completion email.

Locks the copy that customers see when a tool run fails:

* Subject line says "failed" (not "ready").
* HTML headline + plain-text headline say "failed" (not "is ready").
* CTA is the muted/grey "View job details" button (not the green "View results").
* Summary tells the user the wallet was not charged when the pipeline produced no work.

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


class TestFailureSummaryMentionsNoCharge:
    def test_no_gpu_time_summary_says_wallet_not_charged(self):
        """When the wallet hold is released, the summary tells the user
        no charge was made."""
        summary = email_mod._result_summary(
            _job(inputs={"_wallet": {"hold_tx_id": "tx-hold-stub"}}),
            tone="failed",
        )
        assert "wallet was not charged" in summary
        assert "run_pipeline exited 1" in summary

    def test_real_gpu_time_summary_skips_no_charge_claim(self):
        """When GPU time was consumed (charge applies), don't claim no
        charge was made."""
        summary = email_mod._result_summary(
            _job(
                inputs={"_wallet": {"hold_tx_id": "tx-hold-stub"}},
                gpu_seconds_used=420,
            ),
            tone="failed",
        )
        assert "wallet was not charged" not in summary
        assert "did not complete" in summary

    def test_no_wallet_hold_skips_no_charge_claim(self):
        """Free smoke tier (no wallet hold) is gated out of the no-charge
        reassurance since no charge was ever possible."""
        summary = email_mod._result_summary(
            _job(inputs={}),
            tone="failed",
        )
        assert "wallet was not charged" not in summary


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

    def test_succeeded_with_zero_designs_is_empty(self):
        """The designs-only shape, which used to email a success.

        opendde, boltz2 and iggm write ``designs``, not ``candidates``.
        _is_empty_result recognised neither that key nor anything else in
        the payload, so a run where every structure upload failed fell
        past every branch to its "treated as a real success" default: the
        customer got a green View results button and "0 candidates
        returned with real scores and downloadable PDBs".

        The payload below is what run_pipeline actually writes in that
        case -- designs_total comes from the job spec, not from
        len(designs_out), so it is 5 while designs is empty.
        """
        job = _job(
            status="succeeded", error=None, tool="opendde",
            result={
                "status": "COMPLETED", "tier": "general",
                "designs_total": 5, "designs_completed": 0,
                "n_failures": 5, "designs": [], "runtime_seconds": 412,
            },
        )
        assert email_mod._result_tone(job) == "empty"
        # Not "the false phrase is absent" -- the empty branch cannot
        # contain it for any input, so that assertion could never fail.
        # Pin the copy that should be there instead.
        assert "no passing candidates" in email_mod._result_summary(
            job, tone="empty",
        )

    def test_succeeded_with_designs_is_success(self):
        """The other side of it: one design must stay a success."""
        job = _job(status="succeeded", error=None, tool="opendde",
                   result={"designs": [{"rank": 0, "pdb_key": "a.pdb"}]})
        assert email_mod._result_tone(job) == "success"
        summary = email_mod._result_summary(job, tone="success")
        # "structures", not "PDBs". boltzgen writes .cif for most rows and
        # reaches this same line; reverting the word was caught by nothing
        # until this assertion.
        assert "downloadable structures" in summary
        assert "downloadable PDBs" not in summary

    def test_an_unreadable_payload_asserts_nothing_about_it(self):
        """The default for a shape _is_empty_result cannot recognise.

        It used to fall through to "0 candidates returned with real scores
        and downloadable PDBs" under a green View results button -- three
        specific claims about a payload the branch exists because it could
        not read. Reachable by construction: webhooks/modal.py,
        blueprints/jobs.py and shared/compute_campaigns.py each coerce a
        missing completion payload to {} on a SUCCEEDED job.
        """
        # A FALSY payload is not a shape question. job_detail.html:282
        # gates the whole results section on `job.result`, so {} and None
        # render no results block at all -- the email must match the page.
        for payload in ({}, None):
            job = _job(status="succeeded", error=None, tool="boltzgen",
                       result=payload)
            assert email_mod._result_tone(job) == "empty", payload
            summary = email_mod._result_summary(job, tone="empty")
            assert summary.startswith(
                "The pipeline finished but produced no passing candidates."
            ), payload

        # Truthy but unreadable: the page DOES render a results block
        # ("Candidates (0)"), so "the results are on the job page" is true
        # here and false for the two above. Pinned exactly, because
        # absence-only assertions let a DIFFERENT false sentence through.
        job = _job(status="succeeded", error=None, tool="boltzgen",
                   result={"tier": "pilot", "runtime_seconds": 10})
        summary = email_mod._result_summary(
            job, tone=email_mod._result_tone(job),
        )
        assert summary == "Your run finished. The results are on the job page."

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
