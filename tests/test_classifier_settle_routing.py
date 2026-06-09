"""Regression coverage for the classifier driven settle routing branch.

PR #17 follow up. The classifier driven block in
``shared.jobs._settle_wallet_hold_for_completed_job`` is exercised here
for the cases not already pinned by sibling test modules:

  * ``test_zero_consumption_settle_routing.py`` covers the zero
    ``gpu_seconds_used`` rows for every BILLED class plus the
    ``user_cancelled`` positive consumption belt and suspenders case.
  * ``test_failure_classifier.py`` covers the pure function mapping
    invariants for ``classify_terminal_state`` and the frozensets.
  * ``test_jobs.py::TestReleaseOnFailure`` covers the legacy heuristic
    fallback path (``failure_class=None`` pre 0029 rows).

This file pins:

  A. BILLED non user_cancelled classes (``succeeded``,
     ``completed_no_yield``, ``safety_kill``) with realistic positive
     ``gpu_seconds_used`` route to ``settle_hold`` with that exact gpu
     value, not ``release_hold``.

  B. REFUNDED classes (``infra_crash``, ``tool_error``,
     ``preflight_miss``, ``no_progress_timeout``, ``unclassified``)
     route to ``release_hold`` and never to ``settle_hold``. The
     critical mirror of B3 case is one REFUNDED row with positive
     ``gpu_seconds_used``: the REFUNDED branch must not consult gpu
     consumption, ever.

  C. A parametrised matrix sweeps all nine classifier enum values with
     a fixed positive gpu_seconds so the full routing table is locked.

Patching note: the production code uses a local import inside
``_settle_wallet_hold_for_completed_job`` (``from shared.wallet import
release_hold, settle_hold``). Late binding resolves against
``shared.wallet``, so the mock targets are
``shared.wallet.release_hold`` and ``shared.wallet.settle_hold``.
Patching ``shared.jobs.release_hold`` would not intercept the call.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from shared import jobs as jobs_mod
from shared.jobs import ToolJob


# ---------------------------------------------------------------------------
# Row + job factories (local copy of the pattern used by sibling tests).
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "bindcraft",
        "preset": "pilot",
        "status": "running",
        "inputs": {},
        "result": None,
        "error": None,
        "modal_function_call_id": "fc-stub-bindcraft-pilot-xyz",
        "job_token": "t" * 64,
        "gpu_seconds_used": None,
        "created_at": "2026-04-24T00:00:00Z",
        "started_at": None,
        "completed_at": None,
        "failure_class": None,
    }
    base.update(over)
    return base


def _wallet_row(**over) -> dict:
    """Row pre stashed with a wallet hold so the settle hook engages."""
    inputs = {
        "_wallet": {
            "hold_tx_id": "tx-hold-classifier-routing",
            "estimate_usd": "4.40",
            "tool_slug": "bindcraft",
        },
        "num_designs": 100,
    }
    over.setdefault("inputs", inputs)
    return _row(**over)


# ---------------------------------------------------------------------------
# A. BILLED non user_cancelled with positive gpu_seconds: settle exactly.
# ---------------------------------------------------------------------------


class TestBilledPositiveGpuRoutesToSettle:
    """BILLED classes other than ``user_cancelled`` with a realistic
    positive ``gpu_seconds_used`` must route to ``settle_hold`` carrying
    that exact gpu value. Each test uses a different positive number so a
    regression on one class is identifiable at a glance."""

    def test_succeeded_with_positive_gpu_settles_with_exact_value(self):
        """A clean ``succeeded`` row with measured GPU consumption goes
        through ``settle_hold``. The kwargs must carry the exact
        gpu_seconds float from the job row."""
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=300,
                failure_class="succeeded",
                result={"gpu_class": "A100-40GB", "candidates": [{"id": "c1"}]},
            )
        )
        with patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()
        release.assert_not_called()
        assert settle.call_args.args[0] == "tx-hold-classifier-routing"
        assert settle.call_args.kwargs["gpu_seconds"] == 300.0
        assert settle.call_args.kwargs["gpu_class"] == "A100-40GB"

    def test_completed_no_yield_with_positive_gpu_settles_with_exact_value(self):
        """``completed_no_yield`` is BILLED: the GPU ran the full job,
        the user is charged for what consumed even when zero candidates
        passed the downstream filters."""
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=450,
                failure_class="completed_no_yield",
                result={"candidates": [], "gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()
        release.assert_not_called()
        assert settle.call_args.kwargs["gpu_seconds"] == 450.0

    def test_safety_kill_with_positive_gpu_settles_with_exact_value(self):
        """``safety_kill`` fires when the server side overrun monitor
        kills a runaway job mid stream. The user is billed for the GPU
        that ran before the kill. ``failure_reason`` carries the bucket
        through to the settle row for the audit trail."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=600,
                failure_class="safety_kill",
                error={"bucket": "overrun_safety_kill", "detail": "ratio 2.0x"},
            )
        )
        with patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()
        release.assert_not_called()
        assert settle.call_args.kwargs["gpu_seconds"] == 600.0
        assert settle.call_args.kwargs["failure_reason"] == "overrun_safety_kill"


# ---------------------------------------------------------------------------
# B. REFUNDED classes route to release_hold regardless of gpu_seconds.
# ---------------------------------------------------------------------------


class TestRefundedClassesRouteToRelease:
    """Every REFUNDED class must route the hold to ``release_hold`` and
    must not call ``settle_hold``. The branch must not consult
    ``gpu_seconds_used`` at all: a refund row with positive consumption
    is still a full refund, by spec."""

    def test_infra_crash_releases_hold(self):
        """``infra_crash`` covers Modal pod death, Supabase Storage
        upload failures, and similar infra side faults. Full refund."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=0,
                failure_class="infra_crash",
                error={"bucket": "modal_crash", "detail": "pod oom"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()
        assert release.call_args.args[0] == "tx-hold-classifier-routing"

    def test_tool_error_releases_hold(self):
        """``tool_error`` covers docker side run_pipeline crashes. The
        tool fault is on the platform, not the user; full refund."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=0,
                failure_class="tool_error",
                error={"bucket": "pipeline", "detail": "run_pipeline exit 1"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()

    def test_preflight_miss_releases_hold(self):
        """``preflight_miss`` is the docker side preflight failure
        bucket. The GPU never did real work; full refund."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=0,
                failure_class="preflight_miss",
                error={"bucket": "preflight", "detail": "missing weights"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()

    def test_no_progress_timeout_releases_hold(self):
        """``no_progress_timeout`` covers Modal side stalls where the
        job stopped emitting heartbeats. Status is ``timeout``."""
        job = ToolJob.from_row(
            _wallet_row(
                status="timeout",
                gpu_seconds_used=0,
                failure_class="no_progress_timeout",
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()
        # Failure reason for a timeout row maps to the literal status.
        assert release.call_args.kwargs["reason"] in {
            "timeout",
            "no_progress_timeout",
        }

    def test_unclassified_releases_hold(self):
        """``unclassified`` is the conservative default for failed rows
        whose error bucket is not in the known table. By policy these
        refund: judgment cases default to refund, not bill."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=0,
                failure_class="unclassified",
                error={"bucket": "novel_bucket_not_in_table"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()


class TestRefundedClassIgnoresPositiveGpu:
    """Symmetric mirror of bug B3 on the BILLED side. The REFUNDED
    branch must refund a full hold regardless of how much GPU the job
    consumed before the failure. A future regression adding a
    'settle anyway if gpu_seconds > 0' line in the REFUNDED branch is
    caught here."""

    def test_infra_crash_with_positive_gpu_still_releases(self):
        """``infra_crash`` with 300 reported gpu_seconds: the GPU ran
        before the pod died, but the user is not billed for an infra
        side fault. ``release_hold`` must fire; ``settle_hold`` must not."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=300,
                failure_class="infra_crash",
                error={"bucket": "modal_crash", "detail": "pod oom mid run"},
                result={"gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()
        assert release.call_args.args[0] == "tx-hold-classifier-routing"


# ---------------------------------------------------------------------------
# C. Parametrised matrix sweeping every classifier enum value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class,status,error,expected",
    [
        ("succeeded",           "succeeded", None,                                   "settle"),
        ("completed_no_yield",  "succeeded", None,                                   "settle"),
        ("safety_kill",         "failed",    {"bucket": "overrun_safety_kill"},      "settle"),
        ("user_cancelled",      "cancelled", None,                                   "settle"),
        ("infra_crash",         "failed",    {"bucket": "modal_crash"},              "release"),
        ("tool_error",          "failed",    {"bucket": "pipeline"},                 "release"),
        ("preflight_miss",      "failed",    {"bucket": "preflight"},                "release"),
        ("no_progress_timeout", "timeout",   None,                                   "release"),
        ("unclassified",        "failed",    {"bucket": "novel_bucket"},             "release"),
    ],
)
def test_classifier_routing_matrix_with_positive_gpu(
    failure_class, status, error, expected
):
    """Sweep every classifier enum value at a fixed positive
    ``gpu_seconds_used`` (300). This is the end to end pin: every
    failure_class produced by ``classify_terminal_state`` drives the
    expected RPC.

    BILLED classes settle_hold with the exact 300 seconds. REFUNDED
    classes release_hold and ignore the 300 seconds entirely. This
    matrix is the single source of truth for the classifier routing
    contract."""
    job = ToolJob.from_row(
        _wallet_row(
            status=status,
            gpu_seconds_used=300,
            failure_class=failure_class,
            error=error,
        )
    )
    with patch("shared.wallet.release_hold") as release, patch(
        "shared.wallet.settle_hold"
    ) as settle:
        jobs_mod._settle_wallet_hold_for_completed_job(job)
    if expected == "settle":
        settle.assert_called_once()
        release.assert_not_called()
        assert settle.call_args.kwargs["gpu_seconds"] == 300.0
    else:
        release.assert_called_once()
        settle.assert_not_called()
