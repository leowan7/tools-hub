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

from shared.jobs import (
    _GENERIC_FIX,
    _NUMERICAL_FIX,
    _NUMERICAL_FIX_SEEDED,
    ToolJob,
    failure_advice,
)
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

# The ``design() raised: `` prefix is verbatim from
# tools/esmfold2_design/run_pipeline.py:1141, which sets ``error`` to that
# plain string. The torch text after it is the detail QA 2026-09-30 P0-4
# observed on job a327d5fe and cannot be checked statically. The
# ``pipeline``/``design`` bucket and check the tests wrap it in are this
# file's assumption about the poll path, not something that emitter stamps.
_ESMFOLD2_SVD_DETAIL = (
    "design() raised: linalg.svd: (Batch element 0): The algorithm failed "
    "to converge because the input matrix is ill-conditioned or has too "
    "many repeated singular values (error code: 3)."
)


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
    # Our parser found no MPNN output: not the user's FASTA. Was "generic"
    # after review round 1 and became "our_side" when the rule took the
    # whole ``parser`` bucket -- see TestSilentStubIsOurSide.
    ("our_side", dict(tool="mpnn", error=_poll_error(
        "parser", "fasta_missing", "expected MPNN FASTA at /tmp/seqs/x.fa"))),
    ("our_side", dict(tool="af2", error=_poll_error(
        "input", "smoke_fixture", "baked smoke fasta missing at /app/x.fa"))),
    # A malformed proteina region string: the chain IS in the file.
    ("generic", dict(tool="proteina", error=_poll_error(
        "input", "target_input", "unparsable target_input segment 'A1'"))),
    ("chain", dict(tool="proteina", error=_poll_error(
        "input", "target_input",
        "chain C is not present in the uploaded target. It contains: A, B"))),
    # The string QA 2026-09-30 P0-4 saw, verbatim from the emitter at
    # tools/esmfold2_design/run_pipeline.py:1141.
    ("numerical", dict(tool="esmfold2-design", error=_poll_error(
        "pipeline", "design",
        "design() raised: linalg.svd: (Batch element 0): The algorithm failed "
        "to converge because the input matrix is ill-conditioned or has too "
        "many repeated singular values (error code: 3)."))),
    ("numerical", dict(tool="boltz2", error={
        "category": "Pipeline error",
        "message": "Pipeline failed: loss became nan at step 12"})),
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


class TestSeedPinnedRunIsNotToldToRetryUnchanged:
    """QA 2026-09-30 P0-4. A numerical stop on a run whose starting seed the
    form pinned was advised "Try again with the same settings", i.e. the one
    thing that does not vary the run's starting point. The advice now names
    the seed, and ``retry_unchanged`` is False so job_detail.html does not
    label the clone button "Try again with these settings" either.

    This does NOT assert that an identical resubmit fails again -- for
    esmfold2-design it need not (docs/VALIDATION-LOG.md:243: two
    byte-identical payloads, 0 of 6 shared sequences).
    """

    _SVD = _ESMFOLD2_SVD_DETAIL

    def _numerical(self, **over):
        return failure_advice(_job(
            tool="esmfold2-design",
            error=_poll_error("pipeline", "design", self._SVD),
            **over,
        ))

    def test_seed_pinned_numerical_failure_names_the_seed(self):
        advice = self._numerical(inputs=dict(_HOLD, seed=0, n_seeds=1))
        assert advice["kind"] == "numerical"
        assert advice["fix"] == _NUMERICAL_FIX_SEEDED
        assert "starting seed" in advice["fix"]

    @pytest.mark.parametrize("seed", [0, 7, "0"])
    def test_no_retry_unchanged_advice_for_a_pinned_seed(self, seed):
        advice = self._numerical(inputs=dict(_HOLD, seed=seed))
        assert advice["retry_unchanged"] is False
        assert "same settings" not in advice["fix"]

    def test_unseeded_tool_keeps_the_plain_retry(self):
        advice = self._numerical(inputs=dict(_HOLD))
        assert advice["fix"] == _NUMERICAL_FIX
        assert advice["retry_unchanged"] is True

    def test_the_generic_fix_is_the_only_same_settings_text(self):
        """``retry_unchanged`` is derived from the fix text, so a new fix that
        asks for an identical resubmit without being one of the two listed
        texts would render the contradictory button.

        Pins both members of the pair: the set of rule fixes that set
        ``retry_unchanged`` is exactly ``{_GENERIC_FIX, _NUMERICAL_FIX}``.
        Outside the pair this only rejects the literal substrings "same
        settings" and "Run it again", so a third retry-style fix worded some
        other way ("Resubmit", "Try again") would pass here: the guard is
        narrower than a general no-unchanged-retry check.
        """
        from shared.jobs import _FAILURE_RULES
        for kind, _pattern, _cause, fix in _FAILURE_RULES:
            if "same settings" in fix:
                assert fix is _GENERIC_FIX, kind
        # Both members reachable from the table, and nothing else that reads
        # like an unchanged retry. _NUMERICAL_FIX_SEEDED is deliberately not
        # in the table: failure_advice swaps it in for a pinned seed.
        pair = {_GENERIC_FIX, _NUMERICAL_FIX}
        fixes = [fix for _k, _p, _c, fix in _FAILURE_RULES]
        assert {f for f in fixes if f in pair} == pair
        for kind, _pattern, _cause, fix in _FAILURE_RULES:
            if fix not in pair:
                assert "Run it again" not in fix, kind
                assert "same settings" not in fix, kind

    def test_a_seed_pinned_page_does_not_offer_an_unchanged_retry(self, client):
        job = _job(tool="esmfold2-design",
                   inputs=dict(_HOLD, seed=0),
                   error=_poll_error("pipeline", "design", self._SVD))
        with patch("shared.wallet.job_spend_by_hold", return_value={}):
            text = _page(client, job)
        assert "Try again with these settings" not in text
        assert "Open this form with these settings" in text
        assert "Change the starting seed" in text
        # The raw torch line is staff-only (tests/test_failed_run_refund_copy.py
        # ::TestJobPage::test_raw_detail_is_staff_only pins both sides).
        assert "linalg.svd" not in text


class TestSilentStubIsOurSide:
    """A ``parser:stub`` failure is our fault, so it must not ask for new input.

    Every ``_fail("parser", "stub", ...)`` site in the repo sits inside a
    function named ``reject_stub`` -- in tools/af2, tools/colabfold,
    tools/esmfold and tools/mpnn run_pipeline.py -- and each rejects the
    TOOL's OWN output, never the submitted input: a pLDDT array that is
    empty, uniform, outside [0, 100], or carrying any NaN or infinity (the
    guards do not require the whole array), a pTM/ipTM head that
    returned exactly zero, an output PDB that will not decode or carries no
    ATOM records, and for mpnn (no pLDDT) sequences that come back
    identical, near-clones or too tightly clustered on score and recovery.
    colabfold's ``reject_stub`` docstring names the NaN case
    as weights never loaded or a wrong dtype.

    Without the ``parser`` alternative on the "our_side" rule, the two
    details below that carry the word NaN match the "numerical" rule and are
    answered with "change the input -- a cleaned structure, or a smaller
    target", which af2 / colabfold / esmfold do not accept: all three carry
    ``requires_pdb=False`` (tools/af2/__init__.py:426,
    tools/colabfold/__init__.py:409, tools/esmfold/__init__.py:298) and fold
    sequences. The other three matched no rule and fell to "generic", whose
    cause sentence does not say the failure was ours. The alternative puts
    all five on one accurate cause.

    ``test_sibling_buckets_are_our_side_too`` covers the same substitution on
    the two neighbouring classes a code review of this change found still
    reachable: the non-``stub`` checks in the same ``parser`` bucket, and
    proteina's ``internal:unhandled_exception``, whose own detail text says
    "This is a bug in run_pipeline.py, not a bad request". The rule takes
    ``parser`` as a whole bucket, so a check added to it later is covered
    without an edit; ``internal`` is taken one check at a time, because its
    sibling ``did_not_complete`` can be an OOM-kill -- see
    ``test_did_not_complete_makes_no_claim_about_input``.
    """

    _STUB_DETAILS = [
        "pLDDT array contains NaN or infinite - AF2 silent-stub signature",
        "plddt has 512/512 NaN entries",
        "pLDDT array is empty after parse",
        "pdb_b64 failed to decode: Invalid base64-encoded string",
        "PDB contains no ATOM records",
    ]

    @pytest.mark.parametrize("detail", _STUB_DETAILS)
    def test_poll_shape_is_our_side(self, detail):
        """The flattened shape: ``"parser:stub — ..."`` with no check key.

        gpu/modal_client.py:795 builds it, blueprints/jobs.py:943 stores it,
        and it is the path these tools take (they return their terminal
        payload inline as smoke_result, not through /webhooks/modal).
        """
        advice = failure_advice(_job(error=_poll_error("parser", "stub", detail)))
        assert advice["kind"] == "our_side"

    @pytest.mark.parametrize("detail", _STUB_DETAILS)
    def test_webhook_shape_is_our_side(self, detail):
        """The raw ``{bucket, check, detail}`` dict webhooks/modal.py:154 stores."""
        advice = failure_advice(_job(
            error={"bucket": "parser", "check": "stub", "detail": detail}))
        assert advice["kind"] == "our_side"

    @pytest.mark.parametrize("detail", _STUB_DETAILS)
    def test_never_asks_a_sequence_tool_to_clean_a_structure(self, detail):
        advice = failure_advice(_job(
            tool="af2", error=_poll_error("parser", "stub", detail)))
        assert "cleaned structure" not in advice["fix"]
        assert "smaller target" not in advice["fix"]

    def test_the_two_stub_siblings_get_one_explanation(self):
        """A NaN-bearing array and an empty array are the same weights fault."""
        kinds = {
            failure_advice(_job(error=_poll_error("parser", "stub", d)))["cause"]
            for d in ("pLDDT array contains NaN or infinite - AF2 silent-stub "
                      "signature", "pLDDT array is empty after parse")
        }
        assert len(kinds) == 1

    # (bucket, check, detail) taken from the emitters: the ten non-stub
    # ``parser`` checks and ``internal:unhandled_exception``. Routing these
    # eleven through the rule table of 582e0ebc sent all eleven to "generic"
    # (measured). Routing them through THIS table with the ``our_side`` rule
    # removed sends two to "numerical" -- the ``_LinAlgError`` text and
    # ``plddt_parse``'s "nan%" -- and nine to "generic" (measured), which is
    # what the ``our_side`` rule and the checks below exist to prevent.
    _SIBLINGS = [
        ("parser", "plddt_parse",
         "plddt values not numeric: could not convert string to float: 'nan%'"),
        ("parser", "plddt_missing", "no plddt key in scores JSON"),
        ("parser", "scores_parse", "failed to read scores.json: Expecting value"),
        ("parser", "scores_missing", "no scores JSON in /out"),
        ("parser", "pdb_missing", "no PDB file in /out"),
        ("parser", "pdb_read", "could not read ranked_0.pdb: [Errno 2]"),
        ("parser", "pae_missing", "no pae key in scores JSON"),
        ("parser", "pdb_tiny", "predicted PDB is 84 bytes"),
        ("parser", "fasta_missing", "expected MPNN FASTA at /tmp/seqs/x.fa"),
        ("parser", "empty", "parsed zero sample sequences from seqs.fa"),
        ("internal", "unhandled_exception",
         "the shard crashed with an unhandled _LinAlgError: linalg.cholesky: "
         "The factorization could not be completed. This is a bug in "
         "run_pipeline.py, not a bad request"),
    ]

    @pytest.mark.parametrize("bucket,check,detail", _SIBLINGS)
    def test_sibling_buckets_are_our_side_too(self, bucket, check, detail):
        """Neither shape may be answered with advice about the user's input."""
        for error in (_poll_error(bucket, check, detail),
                      {"bucket": bucket, "check": check, "detail": detail}):
            advice = failure_advice(_job(error=error))
            assert advice["kind"] == "our_side", error
            assert "cleaned structure" not in advice["fix"]
            assert "smaller target" not in advice["fix"]

    def test_the_word_parser_in_a_detail_is_not_a_bucket(self):
        """Only the bucket field may match, not the word in someone's prose.

        ``shared/jobs.py::_error_text`` joins bucket first, so the
        ``parser`` alternative is anchored with ``^``. mpnn's
        ``verify:fixed_positions`` detail contains the word "parser"
        (tools/mpnn/run_pipeline.py:869-872) and must keep its own class.
        """
        detail = ("residue counts disagree with MPNN's parser for chain A: "
                  "ours=120, mpnn=118 - the 1-indexed positions were "
                  "bounds-checked against the wrong lengths, so the freeze "
                  "cannot be trusted")
        advice = failure_advice(_job(
            tool="mpnn", error=_poll_error("verify", "fixed_positions", detail)))
        assert advice["kind"] != "our_side"

    def test_did_not_complete_makes_no_claim_about_input(self):
        """An OOM-kill is not "not because of your input".

        The emitter's own comment (tools/proteina/run_pipeline.py, the
        ``did_not_complete`` dict) names an OOM-kill or a full /tmp among
        the causes and refuses to assert which. An OOM-kill follows from
        how large a target was submitted, so this check must not get the
        "our_side" cause, which says the input was not the reason.
        """
        for error in (_poll_error("internal", "did_not_complete",
                                  "the shard exited before writing a result"),
                      {"bucket": "internal", "check": "did_not_complete",
                       "detail": "the shard exited before writing a result"}):
            advice = failure_advice(_job(tool="proteina", error=error))
            assert advice["kind"] != "our_side", error
            assert "not because of your input" not in advice["cause"]


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
        assert ("Try again with these settings" if advice["retry_unchanged"]
                else "Open this form with these settings") in text
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
