"""Regression coverage for the classifier driven settle routing branch.

The parametrised matrix at the bottom is the single source of truth for
the routing table: every ``failure_class`` enum value is exercised at
gpu=300 and asserted against the expected wallet RPC. Two named tests
above the matrix pin kwargs passthrough facts the matrix itself does not
assert (``failure_reason`` into ``settle_hold``; ``reason`` into
``release_hold``).

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
# Kwargs passthrough pins (facts the matrix sweep does not carry).
# ---------------------------------------------------------------------------


def test_safety_kill_passes_failure_reason_to_settle_hold():
    """``safety_kill`` routes to settle_hold and the overrun bucket flows
    through as the ``failure_reason`` kwarg for the audit trail."""
    job = ToolJob.from_row(
        _wallet_row(
            status="failed",
            gpu_seconds_used=600,
            failure_class="safety_kill",
            error={"bucket": "overrun_safety_kill"},
        )
    )
    with patch("shared.wallet.settle_hold") as settle, patch(
        "shared.wallet.release_hold"
    ) as release:
        jobs_mod._settle_wallet_hold_for_completed_job(job)
    settle.assert_called_once()
    release.assert_not_called()
    assert settle.call_args.kwargs["failure_reason"] == "overrun_safety_kill"


def test_no_progress_timeout_passes_reason_to_release_hold():
    """``no_progress_timeout`` routes to release_hold and the timeout
    status flows through as the ``reason`` kwarg on the refund."""
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
    assert release.call_args.kwargs["reason"] in {
        "timeout",
        "no_progress_timeout",
    }


# ---------------------------------------------------------------------------
# Parametrised matrix sweeping every classifier enum value at gpu=300.
# BILLED rows first, then REFUNDED rows.
# B3 mirror: REFUNDED classes ignore gpu_seconds. Every REFUNDED row
# below runs at gpu=300 and must still release_hold (never settle).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class,status,error,expected",
    [
        # BILLED rows
        ("succeeded",           "succeeded", None,                                   "settle"),
        ("completed_no_yield",  "succeeded", None,                                   "settle"),
        ("safety_kill",         "failed",    {"bucket": "overrun_safety_kill"},      "settle"),
        ("user_cancelled",      "cancelled", None,                                   "settle"),
        # REFUNDED rows (B3 mirror: gpu=300 is ignored)
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
    """Sweep every classifier enum value at gpu_seconds_used=300.

    BILLED classes route to settle_hold with that exact 300 seconds.
    REFUNDED classes route to release_hold and ignore the 300 seconds
    entirely (B3 mirror). The hold tx id is asserted once here as a
    sanity that the right wallet hold is acted on."""
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
        assert settle.call_args.args[0] == "tx-hold-classifier-routing"
        assert settle.call_args.kwargs["gpu_seconds"] == 300.0
    else:
        release.assert_called_once()
        settle.assert_not_called()


# ---------------------------------------------------------------------------
# Defensive fall through: unknown failure_class string.
# ---------------------------------------------------------------------------


def test_unknown_failure_class_falls_through_to_legacy_heuristic():
    """A failure_class string outside both BILLED and REFUNDED frozensets
    must not raise; the defensive branch logs and falls through to the
    legacy status driven heuristic. The DB CHECK constraint prevents
    this in practice, so the row is fabricated.

    Legacy heuristic with status='failed' and gpu_seconds>0 settles.
    """
    job = ToolJob.from_row(
        _wallet_row(
            status="failed",
            gpu_seconds_used=300,
            failure_class="totally_made_up",
        )
    )
    with patch("shared.wallet.release_hold") as release, patch(
        "shared.wallet.settle_hold"
    ) as settle:
        jobs_mod._settle_wallet_hold_for_completed_job(job)
    settle.assert_called_once()
    release.assert_not_called()
    assert settle.call_args.kwargs["gpu_seconds"] == 300.0
