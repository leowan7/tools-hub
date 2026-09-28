"""The failed / timed-out job page: what went wrong, what to change, the
refund read from the ledger, a retry link, and the "What next?" links.

Failure strings below are copied from their emitters (``_fail`` calls in
tools/*/run_pipeline.py, the webhook path in webhooks/modal.py, the
tool_submit failures in blueprints/tools.py) so each rule is exercised on
text the app can actually store.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from shared.jobs import ToolJob, failure_advice
from tests.test_clone_roundtrip import (  # noqa: F401 - tools_app is a fixture
    _clone_html,
    _posted_value,
    _stored_inputs,
    tools_app,
)
from tests.test_failed_run_refund_copy import _page, client  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_JID = str(uuid.uuid4())
_HOLD = {"_wallet": {"hold_tx_id": "tx-hold-stub"}}


def _job(**over) -> ToolJob:
    base = {
        "id": _JID,
        "user_id": str(uuid.uuid4()),
        "tool": "bindcraft",
        "preset": "pilot",
        "status": "failed",
        "failure_class": "unclassified",
        "inputs": dict(_HOLD),
        "result": None,
        "error": None,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 131,
        "created_at": "2026-09-27T12:00:00Z",
        "started_at": None,
        "completed_at": "2026-09-27T13:00:00Z",
    }
    base.update(over)
    return ToolJob.from_row(base)


def _poll_error(bucket: str, check: str, detail: str) -> dict:
    """The shape blueprints/jobs.py stores from a poll-path failure."""
    return {"bucket": bucket, "detail": f"{bucket}:{check} — {detail}"}


# (expected kind, job overrides). One real emitter string per class.
_CASES = [
    ("our_side", dict(failure_class="infra_crash",
                      error={"bucket": "modal-submit", "detail": "spawn failed"})),
    ("our_side", dict(error=_poll_error("input", "download", "antigen PDB download failed"))),
    ("our_side", dict(error={"category": "Pipeline error",
                             "message": "Pipeline failed: Failed to get upload URLs: 500"})),
    ("our_side", dict(failure_class="tool_error",
                      error=_poll_error("preflight", "cuda", "no CUDA device visible"))),
    ("timeout", dict(status="timeout", failure_class="no_progress_timeout", error=None)),
    ("timeout", dict(failure_class="tool_error",
                     error=_poll_error("tool-invocation", "boltzgen_timeout", "exceeded 3600s"))),
    ("hotspot", dict(error=_poll_error(
        "input", "epitope",
        "epitope residue number(s) 999 are not on antigen chain A. "
        "Pick epitope residues on the antigen."))),
    ("hotspot", dict(error=_poll_error("input", "hotspot_malformed", "bad token 'x12'"))),
    ("chain", dict(error=_poll_error(
        "input", "antigen_chain", "chain 'Z' produced 0 residues in the uploaded PDB"))),
    ("chain", dict(error=_poll_error(
        "input", "target_input",
        "chain C is not present in the uploaded target. It contains: A, B"))),
    ("size", dict(error=_poll_error(
        "input", "antigen_size",
        "antigen chain A is 2400 aa, over the 1500 aa limit. Trim the antigen and resubmit."))),
    ("structure", dict(error={"category": "Pipeline error",
                              "message": "Pipeline failed: RFdiffusion produced a degenerate "
                                         "frame mid-denoise (Non-positive determinant)"})),
    ("structure", dict(error={"category": "Pipeline error",
                              "message": "Pipeline failed: PDB sanitize failed: bad record"})),
    ("sequence", dict(tool="af2", error=_poll_error("input", "fasta_empty",
                                                    "FASTA contains zero records"))),
    ("generic", dict(error={"category": "unknown",
                            "message": "Pipeline reported FAILED with no error detail."})),
    ("generic", dict(failure_class="tool_error",
                     error=_poll_error("pipeline", "no_designs", "no designs passed filters"))),
]


class TestFailureAdvice:
    @pytest.mark.parametrize("kind,over", _CASES)
    def test_each_emitted_string_maps_to_its_class(self, kind, over):
        advice = failure_advice(_job(**over))
        assert advice["kind"] == kind, advice
        assert advice["cause"] and advice["fix"]

    def test_download_failure_is_our_side_not_bad_input(self):
        """``input:download`` is our storage; the sequence rule would also
        match "FASTA download failed", so rule order is load-bearing."""
        advice = failure_advice(_job(tool="af2", error=_poll_error(
            "input", "download", "FASTA download failed")))
        assert advice["kind"] == "our_side"

    def test_generic_uses_the_class_sentence(self):
        advice = failure_advice(_job(failure_class="tool_error",
                                     error={"bucket": "pipeline", "detail": "boom"}))
        assert advice["kind"] == "generic"
        assert "hit an error while running" in advice["cause"]

    @pytest.mark.parametrize("status", ["succeeded", "running", "pending", "cancelled"])
    def test_not_shown_outside_failed_or_timeout(self, status):
        assert failure_advice(_job(status=status)) is None


def _ledger(usd, settled, held):
    return {"tx-hold-stub": {"usd": Decimal(usd), "settled": settled, "held": Decimal(held)}}


class TestJobPage:
    @pytest.mark.parametrize("kind,over", _CASES)
    def test_each_class_renders_cause_fix_and_retry(self, client, kind, over):
        job = _job(**over)
        advice = failure_advice(job)
        with patch("shared.wallet.job_spend_by_hold", return_value={}):
            text = _page(client, job)
        assert advice["cause"] in text
        assert "What to change: " + advice["fix"] in text
        assert "Try again with these settings" in text
        assert "What next?" in text

    def test_refund_in_full_is_stated_from_the_ledger(self, client):
        with patch("shared.wallet.job_spend_by_hold",
                   return_value=_ledger("0", True, "4.50")):
            text = _page(client, _job())
        assert "The $4.50 hold was returned to your wallet in full." in text
        assert "You were not charged for this run." in text

    def test_charged_run_is_not_called_refunded(self, client):
        with patch("shared.wallet.job_spend_by_hold",
                   return_value=_ledger("1.25", True, "4.50")):
            text = _page(client, _job())
        assert "You were charged $1.25 for the GPU time this run used." in text
        assert "The rest of the $4.50 hold was returned to your wallet." in text
        assert "not charged" not in text

    def test_unsettled_hold_says_so(self, client):
        with patch("shared.wallet.job_spend_by_hold",
                   return_value=_ledger("4.50", False, "4.50")):
            text = _page(client, _job())
        assert "$4.50 is still on hold for this run and has not been settled yet." in text
        assert "not charged" not in text

    def test_failed_ledger_lookup_makes_no_money_claim(self, client):
        with patch("shared.wallet.job_spend_by_hold", return_value={}):
            text = _page(client, _job())
        assert "not charged" not in text
        assert "You were charged" not in text

    def test_no_hold_skips_the_ledger(self, client):
        with patch("shared.wallet.job_spend_by_hold") as spend:
            text = _page(client, _job(inputs={}))
        spend.assert_not_called()
        assert "not charged" not in text

    def test_retry_link_is_the_clone_url(self, client):
        from types import SimpleNamespace
        ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100,
                              email="u@example.com")
        with client.session_transaction() as sess:
            sess["user_id"] = "u-1"
            sess["user_email"] = "u@example.com"
        with patch("shared.wallet.job_spend_by_hold", return_value={}),                 patch("blueprints.jobs.get_job", return_value=_job()),                 patch("blueprints.jobs.load_user_context", return_value=ctx):
            html = client.get(f"/jobs/{_JID}").get_data(as_text=True)
        assert (f'href="/tools/bindcraft?clone_from={_JID}" class="btn-primary" '
                f'data-retry-link') in html


class TestRetryPrefill:
    """The retry link is ``?clone_from=``; this pins that the form it opens
    carries the failed job's target, chain, hotspots and preset."""

    def test_bindcraft(self, tools_app):
        flask_app, adapters = tools_app
        adapter = next(a for a in adapters if a.slug == "bindcraft")
        inputs = {
            "preset": "pilot",
            "target_chain": "B",
            "hotspot_residues": "54,56,115",
            "binder_length_min": 60,
            "binder_length_max": 90,
            "num_designs": 4,
            "_pdb_storage_path": "u-1/abc/target.pdb",
            "_pdb_filename": "target.pdb",
        }
        html = _clone_html(flask_app, adapter, inputs)
        assert _posted_value(html, "reuse_pdb_token") == "job:job-1234abcd"
        assert _posted_value(html, "target_chain") == "B"
        assert _posted_value(html, "hotspot_residues") == "54,56,115"
        assert _posted_value(html, "preset") == "pilot"
        assert _posted_value(html, "num_designs") == "4"

    def test_af2(self, tools_app):
        flask_app, adapters = tools_app
        adapter = next(a for a in adapters if a.slug == "af2")
        inputs = _stored_inputs(adapter)
        html = _clone_html(flask_app, adapter, inputs)
        assert _posted_value(html, "preset") == inputs["preset"]
        assert _posted_value(html, "num_recycles") == str(inputs["num_recycles"])
        fasta = _posted_value(html, "fasta") or _posted_value(html, "fasta_text") or ""
        assert "QVQLVESGGG" in fasta and "DIQMTQSPSS" in fasta
