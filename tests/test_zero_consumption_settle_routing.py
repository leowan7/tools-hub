"""Regression coverage for the zero consumption BILLED routing rule.

Bug B3 (PR #17 review): the classifier driven block in
``shared.jobs._settle_wallet_hold_for_completed_job`` had an unscoped
zero gpu_seconds fast path that routed every BILLED class to
``release_hold`` when consumption was zero. The inline comment claimed
the path was for ``user_cancelled before the first heartbeat`` but the
condition was ``gpu_seconds <= 0`` only, so a ``succeeded`` /
``completed_no_yield`` / ``safety_kill`` webhook payload that arrived
without runtime fields silently issued a full refund instead of
recording an authoritative zero settlement.

The fix narrows the fast path to ``failure_class == 'user_cancelled'``.
These tests pin that contract:

  * ``succeeded`` with zero gpu_seconds  -> settle_hold(0, ...)
  * ``completed_no_yield`` with zero     -> settle_hold(0, ...)
  * ``safety_kill`` with zero            -> settle_hold(0, ...)
  * ``user_cancelled`` with zero         -> release_hold(...)

The test file deliberately constructs ``ToolJob`` rows by hand and
patches ``shared.wallet.release_hold`` / ``shared.wallet.settle_hold``
so the assertions are about which RPC the routing chooses, not about
the SQL settle math.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from shared import jobs as jobs_mod
from shared.jobs import ToolJob


# ---------------------------------------------------------------------------
# Row + job factories (small, local copy of the test_jobs.py pattern).
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
        "modal_function_call_id": "fc-stub-bindcraft-pilot-abc",
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
            "hold_tx_id": "tx-hold-zero-consumption",
            "estimate_usd": "4.40",
            "tool_slug": "bindcraft",
        },
        "num_designs": 100,
    }
    over.setdefault("inputs", inputs)
    return _row(**over)


# ---------------------------------------------------------------------------
# BILLED non user cancel classes with zero consumption: settle_hold(0).
# ---------------------------------------------------------------------------


class TestBilledZeroConsumptionSettlesAtZero:
    """A BILLED class other than user_cancelled, arriving with zero
    gpu_seconds, must route to settle_hold(gpu_seconds=0, ...) so the
    audit row records an authoritative zero settlement. The pre fix
    code refunded via release_hold, which let a runtime free webhook
    silently overturn the bill."""

    def test_succeeded_with_zero_gpu_seconds_settles_at_zero(self):
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=0,
                failure_class="succeeded",
                result={"gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()
        release.assert_not_called()
        args = settle.call_args.args
        kwargs = settle.call_args.kwargs
        assert args[0] == "tx-hold-zero-consumption"
        assert kwargs["gpu_seconds"] == 0.0
        # underscore prefixed inputs are scrubbed before the cap math runs.
        assert "num_designs" in kwargs["params"]
        assert "_wallet" not in kwargs["params"]

    def test_succeeded_with_none_gpu_seconds_settles_at_zero(self):
        """``gpu_seconds_used`` arrives as NULL when the webhook payload
        omits the runtime field. The settle hook coerces NULL to 0.0 and
        must still hit settle_hold, not release_hold."""
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=None,
                failure_class="succeeded",
            )
        )
        with patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()
        release.assert_not_called()
        assert settle.call_args.kwargs["gpu_seconds"] == 0.0

    def test_completed_no_yield_with_zero_gpu_seconds_settles_at_zero(self):
        """``completed_no_yield`` is a BILLED class: the GPU ran as ordered
        even though zero candidates passed. Zero consumption is a payload
        oddity, not a refund signal."""
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=0,
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
        assert settle.call_args.kwargs["gpu_seconds"] == 0.0

    def test_safety_kill_with_zero_gpu_seconds_settles_at_zero(self):
        """``safety_kill`` fires when the server side overrun monitor
        kills a runaway job. The user is billed for what consumed; if
        the kill happened mid heartbeat and the persisted gpu_seconds is
        still zero, the audit row should be a zero settle not a refund."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=0,
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
        assert settle.call_args.kwargs["gpu_seconds"] == 0.0
        # failure_reason carries through to the settle row.
        assert settle.call_args.kwargs["failure_reason"] == "overrun_safety_kill"


# ---------------------------------------------------------------------------
# user_cancelled with zero consumption: release_hold (typed reason).
# ---------------------------------------------------------------------------


class TestUserCancelledZeroConsumptionStillReleases:
    """The original audit row preference still holds for user cancels:
    a zero consumption ``user_cancelled`` row gets the richer typed
    reason via release_hold rather than a settle_hold(0, ...) notes
    string."""

    def test_user_cancelled_with_zero_gpu_seconds_releases_hold(self):
        job = ToolJob.from_row(
            _wallet_row(
                status="cancelled",
                gpu_seconds_used=0,
                failure_class="user_cancelled",
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()
        args = release.call_args.args
        assert args[0] == "tx-hold-zero-consumption"
        # The typed reason is the audit row payoff. The settle path
        # buries the same string in a notes field; release_hold keeps
        # it as a first class column.
        kwargs = release.call_args.kwargs
        assert kwargs["reason"] in {"cancelled", "user_cancelled"}

    def test_user_cancelled_with_consumed_gpu_still_settles(self):
        """Belt and suspenders: a user_cancelled row that actually
        consumed GPU time bills for it via settle_hold (the cap math
        clamps to the parameter scaled hard cap). The fast path is
        scoped to zero consumption only."""
        job = ToolJob.from_row(
            _wallet_row(
                status="cancelled",
                gpu_seconds_used=120,
                failure_class="user_cancelled",
                result={"gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()
        release.assert_not_called()
        assert settle.call_args.kwargs["gpu_seconds"] == 120.0


# ---------------------------------------------------------------------------
# Parametrised sweep over the full BILLED set for clarity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class,expected",
    [
        ("succeeded", "settle"),
        ("completed_no_yield", "settle"),
        ("safety_kill", "settle"),
        ("user_cancelled", "release"),
    ],
)
def test_billed_classes_zero_consumption_routing_matrix(failure_class, expected):
    """Every BILLED class is exercised once at zero gpu_seconds. Only
    user_cancelled takes the release_hold fast path; the others must
    settle at zero so the audit row records an authoritative zero
    consumption bill instead of a silent refund."""
    # Pick a terminal status that fits the class. Anything in the
    # terminal set works because the routing key is failure_class, not
    # status, once the classifier column is populated.
    status = "cancelled" if failure_class == "user_cancelled" else "succeeded"
    job = ToolJob.from_row(
        _wallet_row(
            status=status,
            gpu_seconds_used=0,
            failure_class=failure_class,
        )
    )
    with patch("shared.wallet.release_hold") as release, patch(
        "shared.wallet.settle_hold"
    ) as settle:
        jobs_mod._settle_wallet_hold_for_completed_job(job)
    if expected == "settle":
        settle.assert_called_once()
        release.assert_not_called()
        assert settle.call_args.kwargs["gpu_seconds"] == 0.0
    else:
        release.assert_called_once()
        settle.assert_not_called()
