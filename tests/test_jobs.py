"""Wallet hold settle plus mid-run monitor tests for shared.jobs.

The Wave 1 settle math (clamp to cap, surplus release, absorbed
variance) is exercised under shared/wallet at the ledger level. These
tests focus on the higher layer in shared/jobs:

* ``_settle_wallet_hold_for_completed_job`` on the succeeded path.
* The same hook on the failed path, both with and without consumed
  GPU time.
* ``mid_run_monitor_check`` for the 1.5x warning (idempotent) and
  the 2.0x safety kill (only when projected cost exceeds the
  parameter scaled hard cap).

The fake job row carries an ``inputs._wallet`` dict so the settle
hook has a hold_tx_id to find. Wallet helpers are patched at the
shared.wallet level so the tests never touch Supabase.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from shared.jobs import ToolJob


# ---------------------------------------------------------------------------
# Fakes (re-used and extended from test_jobs_phase4)
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
        "credits_cost": 22,
        "modal_function_call_id": "fc-stub-bindcraft-pilot-abc",
        "job_token": "t" * 64,
        "gpu_seconds_used": None,
        "created_at": "2026-04-24T00:00:00Z",
        "started_at": None,
        "completed_at": None,
    }
    base.update(over)
    return base


def _wallet_row(**over) -> dict:
    """Build a tool_jobs row pre-stashed with a wallet hold."""
    inputs = {
        "_wallet": {
            "hold_tx_id": "tx-hold-001",
            "estimate_usd": "4.40",
            "tool_slug": "bindcraft",
        },
        "num_designs": 100,
    }
    over.setdefault("inputs", inputs)
    return _row(**over)


# ===========================================================================
# settle on completion
# ===========================================================================


class TestSettleOnCompletion:
    """Successful jobs route through settle_hold with the consumed compute."""

    def test_succeeded_calls_settle_hold_with_gpu_seconds(self):
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=1200,
                result={"candidates": [], "gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()
        release.assert_not_called()
        kwargs = settle.call_args.kwargs
        args = settle.call_args.args
        # hold_tx_id is the first positional argument.
        assert args[0] == "tx-hold-001"
        assert kwargs["gpu_seconds"] == 1200.0
        assert kwargs["gpu_class"] == "A100-40GB"
        assert kwargs["failure_reason"] is None
        # The settle path passes job inputs minus underscore keys so
        # compute_hard_cap can scale on real tool params.
        assert "num_designs" in kwargs["params"]
        assert "_wallet" not in kwargs["params"]

    def test_actual_below_hold_returns_surplus_via_settle(self):
        """settle_hold itself routes surplus into a hold_release. The
        job level hook just makes sure settle_hold is invoked with
        the real consumed time so the SQL layer can compute the
        surplus."""
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=600,
                result={"gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.settle_hold") as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        # 600s on A100-40GB at $0.000714 with 1.7x markup = ~$0.73 actual
        # < $4.40 estimate. The settle path forwards the gpu_seconds and
        # lets the SQL function compute the surplus release.
        assert settle.call_args.kwargs["gpu_seconds"] == 600.0

    def test_actual_above_hold_within_balance_records_charge(self):
        """When actual > hold, settle_hold inserts a charge row for the
        variance. The hook simply forwards gpu_seconds; the SQL layer
        clamps to the parameter scaled hard cap."""
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=10_000,
                result={"gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.settle_hold") as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        assert settle.call_count == 1
        # The wallet ledger tests assert the variance debit / absorbed
        # split. Here we just confirm settle_hold was the chosen RPC,
        # not release_hold.

    def test_actual_far_above_hard_cap_writes_absorbed_variance(self):
        """When actual >> hold and balance has no slack, settle_hold
        inserts an absorbed_variance row. The hook simply forwards the
        real gpu_seconds so the SQL layer can decide."""
        job = ToolJob.from_row(
            _wallet_row(
                status="succeeded",
                gpu_seconds_used=500_000,  # giant overrun
                result={"gpu_class": "A100-40GB"},
            )
        )
        with patch("shared.wallet.settle_hold") as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        settle.assert_called_once()


# ===========================================================================
# release on failure
# ===========================================================================


class TestReleaseOnFailure:
    def test_failed_with_no_gpu_time_releases_hold(self):
        """System failure path: pipeline never ran, so release the hold."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=0,
                error={"bucket": "pipeline", "detail": "early crash"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once_with("tx-hold-001", reason="pipeline")
        settle.assert_not_called()

    def test_failed_with_gpu_time_settles_hold(self):
        """Real GPU consumed before failure: charge for the compute."""
        job = ToolJob.from_row(
            _wallet_row(
                status="failed",
                gpu_seconds_used=300,
                result={"gpu_class": "A100-40GB"},
                error={"bucket": "pipeline", "detail": "fold step failed"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        # When gpu_seconds > 0 we settle (and charge for real compute).
        settle.assert_called_once()
        release.assert_not_called()
        # failure_reason carries through to settle_hold.
        assert settle.call_args.kwargs["failure_reason"] == "pipeline"

    def test_timeout_with_no_gpu_time_releases(self):
        job = ToolJob.from_row(_wallet_row(status="timeout", gpu_seconds_used=0))
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once_with("tx-hold-001", reason="timeout")
        settle.assert_not_called()

    def test_cancelled_releases_hold(self):
        job = ToolJob.from_row(
            _wallet_row(
                status="cancelled",
                gpu_seconds_used=0,
                error={"bucket": "cancelled", "detail": "user_cancelled"},
            )
        )
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_called_once()
        settle.assert_not_called()

    def test_no_wallet_ctx_is_noop(self):
        """Jobs submitted before the wallet pivot have no _wallet key."""
        job = ToolJob.from_row(_row(status="succeeded", gpu_seconds_used=100))
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_not_called()
        settle.assert_not_called()

    def test_non_terminal_status_is_noop(self):
        """Running / pending rows must not trip the settle path."""
        job = ToolJob.from_row(_wallet_row(status="running", gpu_seconds_used=0))
        with patch("shared.wallet.release_hold") as release, patch(
            "shared.wallet.settle_hold"
        ) as settle:
            jobs_mod._settle_wallet_hold_for_completed_job(job)
        release.assert_not_called()
        settle.assert_not_called()


# ===========================================================================
# mid-run monitor
# ===========================================================================


@pytest.fixture
def fake_job_store(monkeypatch):
    """Patch get_job and update_inputs to read/write a single in memory row."""

    rows: dict[str, dict] = {}

    def fake_get_job(job_id, **kwargs):  # noqa: ARG001
        row = rows.get(job_id)
        if not row:
            return None
        return ToolJob.from_row(row)

    def fake_update_inputs(job_id, inputs):
        if job_id in rows:
            rows[job_id]["inputs"] = inputs
            return True
        return False

    def fake_mark_failed(job_id, *, error, gpu_seconds_used=None, **_kwargs):
        if job_id in rows:
            rows[job_id]["status"] = "failed"
            rows[job_id]["error"] = error
            rows[job_id]["gpu_seconds_used"] = gpu_seconds_used
        return True

    monkeypatch.setattr(jobs_mod, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_mod, "update_inputs", fake_update_inputs)
    monkeypatch.setattr(jobs_mod, "mark_failed", fake_mark_failed)
    return rows


def _seed_running(rows, **over):
    """Insert a running job with a wallet hold into the fake store."""
    job_id = str(uuid.uuid4())
    inputs = {
        "_wallet": {
            "hold_tx_id": "tx-running",
            "estimate_usd": "4.40",
            "tool_slug": "bindcraft",
            "gpu_class": "A100-40GB",
        },
        "num_designs": 100,
    }
    row = _row(
        id=job_id,
        status="running",
        inputs=inputs,
        tool="bindcraft",
        gpu_seconds_used=None,
    )
    row.update(over)
    rows[job_id] = row
    return job_id


class TestMidRunMonitorWarn:
    def test_below_warn_ratio_returns_none(self, fake_job_store):
        """A normal still running job at 50% of estimate is a no op."""
        job_id = _seed_running(fake_job_store)
        # cumulative cost = 0.5 * estimate. Use very few gpu_seconds.
        result = jobs_mod.mid_run_monitor_check(job_id, 100.0)
        assert result is None

    def test_warn_ratio_dispatches_email(self, fake_job_store):
        job_id = _seed_running(fake_job_store)
        # 4.40 estimate at A100-40GB, $0.000714/s * 1.7 markup = $0.001214/s
        # To hit 1.5x = $6.60 we need ~5436 gpu_seconds.
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="x@e.com"
        ), patch("shared.email.send_job_capped_email") as cap_email:
            result = jobs_mod.mid_run_monitor_check(job_id, 5500.0)
        assert result == "warned"
        cap_email.assert_called_once()
        # The warning persists the overrun_warned flag so a second
        # heartbeat at the same ratio does not re-dispatch.
        row = fake_job_store[job_id]
        assert row["inputs"]["_wallet"]["overrun_warned"] is True

    def test_warn_is_idempotent(self, fake_job_store):
        job_id = _seed_running(fake_job_store)
        # Pre-set the warned flag.
        fake_job_store[job_id]["inputs"]["_wallet"]["overrun_warned"] = True
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="x@e.com"
        ), patch("shared.email.send_job_capped_email") as cap_email:
            result = jobs_mod.mid_run_monitor_check(job_id, 5500.0)
        # Already warned: no new dispatch and no return label.
        cap_email.assert_not_called()
        assert result is None


class TestMidRunMonitorKill:
    def test_kill_ratio_with_cost_below_cap_no_op(self, fake_job_store):
        """At 2x estimate but still inside the parameter scaled hard cap
        the monitor does not kill the job. The hold itself will absorb
        the variance via settle_hold when the job lands."""
        job_id = _seed_running(fake_job_store)
        # Drop num_designs back to the bindcraft baseline of 2 so the
        # hard cap stays at the base $8 (compute_hard_cap scales it
        # by num_designs/baseline).
        fake_job_store[job_id]["inputs"]["num_designs"] = 2
        # 1.00 estimate; 2x = $2.00 well below $8 cap. 2000s on
        # A100-40GB at 0.001214/s with markup is ~$2.43: above 2x,
        # still below cap, so no kill.
        fake_job_store[job_id]["inputs"]["_wallet"]["estimate_usd"] = "1.00"
        modal = MagicMock()
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="x@e.com"
        ), patch("shared.email.send_job_capped_email"):
            result = jobs_mod.mid_run_monitor_check(
                job_id, 2000.0, modal_client=modal,
            )
        # No kill because cumulative_cost is below the per tool hard cap.
        assert result != "killed"
        modal.cancel.assert_not_called()

    def test_kill_ratio_above_cap_cancels_modal(self, fake_job_store):
        """When cumulative cost exceeds both 2x estimate AND the cap,
        the monitor cancels Modal and flips the job to failed."""
        job_id = _seed_running(fake_job_store)
        # baseline params: hard cap stays at $8 for bindcraft.
        fake_job_store[job_id]["inputs"]["num_designs"] = 2
        fake_job_store[job_id]["inputs"]["_wallet"]["estimate_usd"] = "0.05"
        # 7000s on A100-40GB gives ~$8.50 cumulative. Ratio is 170x;
        # cost is over the $8 cap. Both kill conditions tripped.
        modal = MagicMock()
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="x@e.com"
        ), patch("shared.email.send_job_capped_email") as cap_email, patch(
            "shared.wallet.release_hold"
        ) as release:
            result = jobs_mod.mid_run_monitor_check(
                job_id, 7000.0, modal_client=modal,
            )
        assert result == "killed"
        modal.cancel.assert_called_once()
        release.assert_called_once_with(
            "tx-running", reason="overrun_safety_kill"
        )
        cap_email.assert_called_once()
        # Job row flipped to failed with the safety-kill bucket.
        assert fake_job_store[job_id]["status"] == "failed"
        assert (
            fake_job_store[job_id]["error"]["bucket"] == "overrun_safety_kill"
        )

    def test_kill_skips_modal_when_no_function_call_id(self, fake_job_store):
        job_id = _seed_running(fake_job_store)
        fake_job_store[job_id]["modal_function_call_id"] = None
        fake_job_store[job_id]["inputs"]["num_designs"] = 2
        fake_job_store[job_id]["inputs"]["_wallet"]["estimate_usd"] = "0.05"
        modal = MagicMock()
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="x@e.com"
        ), patch("shared.email.send_job_capped_email"), patch(
            "shared.wallet.release_hold"
        ):
            result = jobs_mod.mid_run_monitor_check(
                job_id, 7000.0, modal_client=modal,
            )
        assert result == "killed"
        modal.cancel.assert_not_called()


class TestMidRunMonitorNoOps:
    def test_unknown_job_is_noop(self, fake_job_store):
        result = jobs_mod.mid_run_monitor_check("no-such-id", 1000.0)
        assert result is None

    def test_already_terminal_is_noop(self, fake_job_store):
        job_id = _seed_running(fake_job_store)
        fake_job_store[job_id]["status"] = "succeeded"
        result = jobs_mod.mid_run_monitor_check(job_id, 5500.0)
        assert result is None

    def test_no_wallet_ctx_is_noop(self, fake_job_store):
        job_id = _seed_running(fake_job_store)
        fake_job_store[job_id]["inputs"].pop("_wallet")
        result = jobs_mod.mid_run_monitor_check(job_id, 5500.0)
        assert result is None

    def test_zero_cumulative_cost_is_noop(self, fake_job_store):
        job_id = _seed_running(fake_job_store)
        result = jobs_mod.mid_run_monitor_check(job_id, 0.0)
        assert result is None


# ===========================================================================
# complete_job invokes the settle hook
# ===========================================================================


class TestCompleteJobInvokesSettle:
    """complete_job must call the new settle hook after the workspace charge."""

    def test_settle_hook_runs_after_succeeded_transition(
        self, monkeypatch
    ):
        rows = {}
        job_id = _seed_running(rows)
        rows[job_id]["status"] = "running"

        def fake_get_job(jid, **kwargs):
            r = rows.get(jid)
            return ToolJob.from_row(r) if r else None

        def fake_mark_succeeded(jid, *, result, gpu_seconds_used, allowed_current):
            if rows[jid]["status"] in allowed_current:
                rows[jid]["status"] = "succeeded"
                rows[jid]["result"] = result
                rows[jid]["gpu_seconds_used"] = gpu_seconds_used
                rows[jid]["completed_at"] = "later"
                return True
            return False

        monkeypatch.setattr(jobs_mod, "get_job", fake_get_job)
        monkeypatch.setattr(jobs_mod, "mark_succeeded", fake_mark_succeeded)
        monkeypatch.setattr(
            jobs_mod, "_refund_unused_credits", lambda j: None
        )
        monkeypatch.setattr(
            jobs_mod, "_charge_workspace_for_completed_job", lambda j: None
        )
        monkeypatch.setattr(
            jobs_mod, "_send_completion_email", lambda j: None
        )

        with patch.object(
            jobs_mod, "_settle_wallet_hold_for_completed_job"
        ) as settle_hook:
            jobs_mod.complete_job(
                job_id,
                terminal_status="succeeded",
                result={"gpu_class": "A100-40GB"},
                gpu_seconds_used=1200,
            )
        settle_hook.assert_called_once()
        invoked_job = settle_hook.call_args.args[0]
        assert invoked_job.status == "succeeded"
        assert invoked_job.gpu_seconds_used == 1200

    def test_settle_hook_runs_after_failed_transition(self, monkeypatch):
        rows = {}
        job_id = _seed_running(rows)

        def fake_get_job(jid, **kwargs):
            r = rows.get(jid)
            return ToolJob.from_row(r) if r else None

        def fake_mark_failed(jid, *, error, gpu_seconds_used=None, allowed_current=None):
            if (
                allowed_current is None
                or rows[jid]["status"] in allowed_current
            ):
                rows[jid]["status"] = "failed"
                rows[jid]["error"] = error
                rows[jid]["gpu_seconds_used"] = gpu_seconds_used
                return True
            return False

        monkeypatch.setattr(jobs_mod, "get_job", fake_get_job)
        monkeypatch.setattr(jobs_mod, "mark_failed", fake_mark_failed)
        monkeypatch.setattr(
            jobs_mod, "_refund_unused_credits", lambda j: None
        )
        monkeypatch.setattr(
            jobs_mod, "_charge_workspace_for_completed_job", lambda j: None
        )
        monkeypatch.setattr(
            jobs_mod, "_send_completion_email", lambda j: None
        )

        with patch.object(
            jobs_mod, "_settle_wallet_hold_for_completed_job"
        ) as settle_hook:
            jobs_mod.complete_job(
                job_id,
                terminal_status="failed",
                error={"bucket": "pipeline", "detail": "boom"},
                gpu_seconds_used=0,
            )
        settle_hook.assert_called_once()
        invoked_job = settle_hook.call_args.args[0]
        assert invoked_job.status == "failed"

    def test_settle_hook_skipped_when_cas_lost(self, monkeypatch):
        """If a concurrent writer terminalised the row, do not settle twice."""
        rows = {}
        job_id = _seed_running(rows)
        # Pretend a peer wrote 'succeeded' before our complete_job lands.
        rows[job_id]["status"] = "succeeded"

        def fake_get_job(jid, **kwargs):
            r = rows.get(jid)
            return ToolJob.from_row(r) if r else None

        monkeypatch.setattr(jobs_mod, "get_job", fake_get_job)
        monkeypatch.setattr(
            jobs_mod, "_refund_unused_credits", lambda j: None
        )
        monkeypatch.setattr(
            jobs_mod, "_charge_workspace_for_completed_job", lambda j: None
        )
        monkeypatch.setattr(
            jobs_mod, "_send_completion_email", lambda j: None
        )
        with patch.object(
            jobs_mod, "_settle_wallet_hold_for_completed_job"
        ) as settle_hook:
            out = jobs_mod.complete_job(
                job_id,
                terminal_status="failed",
                error={"detail": "x"},
            )
        # Already terminal short circuit: returns the row without
        # running any post completion hooks.
        settle_hook.assert_not_called()
        assert out.status == "succeeded"
