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
            # Mirror the real mark_failed: the classifier writes
            # failure_class so the post-mark settle path routes correctly
            # (safety_kill is BILLED, so the hold must settle_hold the
            # consumed GPU, not release_hold a full refund).
            rows[job_id]["failure_class"] = jobs_mod.classify_terminal_state(
                status="failed", error=error,
            )
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
        with patch("shared.email.send_overrun_warning_email") as warn_email:
            result = jobs_mod.mid_run_monitor_check(job_id, 5500.0)
        assert result == "warned"
        warn_email.assert_called_once()
        # The warning persists the overrun_warned flag so a second
        # heartbeat at the same ratio does not re-dispatch.
        row = fake_job_store[job_id]
        assert row["inputs"]["_wallet"]["overrun_warned"] is True

    def test_warn_is_idempotent(self, fake_job_store):
        job_id = _seed_running(fake_job_store)
        # Pre-set the warned flag.
        fake_job_store[job_id]["inputs"]["_wallet"]["overrun_warned"] = True
        with patch("shared.email.send_overrun_warning_email") as warn_email:
            result = jobs_mod.mid_run_monitor_check(job_id, 5500.0)
        # Already warned: no new dispatch and no return label.
        warn_email.assert_not_called()
        assert result is None


class TestMidRunMonitorNoCostKill:
    """The cost-based mid-run kill was removed: a runaway-cost job is
    never terminated mid-run. Spend is bounded by the prepaid wallet +
    per-job hold and wall-clock by the Modal container hard timeout, so
    the monitor never cancels Modal, never flips the job to failed, and
    never settles a hold inline. The terminal path owns settlement.
    """

    def test_runaway_cost_does_not_kill_or_cancel(self, fake_job_store):
        """Cumulative cost far above the old 2x kill ratio and any prior
        hard cap: the monitor must NOT cancel Modal, mark the job failed,
        or settle a hold. It only warns (never kills)."""
        job_id = _seed_running(fake_job_store)
        # baseline params + tiny estimate so the ratio is ~170x, well past
        # the old kill threshold.
        fake_job_store[job_id]["inputs"]["num_designs"] = 2
        fake_job_store[job_id]["inputs"]["_wallet"]["estimate_usd"] = "0.05"
        modal = MagicMock()
        with patch("shared.email.send_overrun_warning_email"), patch(
            "shared.wallet.settle_hold"
        ) as settle, patch("shared.wallet.release_hold") as release:
            result = jobs_mod.mid_run_monitor_check(
                job_id, 7000.0, modal_client=modal,
            )
        # No kill: never returns "killed".
        assert result != "killed"
        # Modal is not cancelled and no terminal/settle side effects fire.
        modal.cancel.assert_not_called()
        settle.assert_not_called()
        release.assert_not_called()
        # The job stays running; the monitor did not flip it to failed.
        assert fake_job_store[job_id]["status"] == "running"

    def test_runaway_cost_above_old_kill_ratio_still_warns(self, fake_job_store):
        """A runaway overrun ABOVE the old 2x kill ratio is no longer
        silent: with the half-open warn band removed the monitor warns
        once (email sent, "warned" returned) instead of killing. This is
        the observability gap (F1) the kill removal opened."""
        job_id = _seed_running(fake_job_store)
        # 0.05 estimate; ~7000 gpu_seconds -> ~170x, far above the old 2.0x
        # kill threshold. Previously this fell through to a silent None
        # (after the kill), now it must warn.
        fake_job_store[job_id]["inputs"]["num_designs"] = 2
        fake_job_store[job_id]["inputs"]["_wallet"]["estimate_usd"] = "0.05"
        modal = MagicMock()
        with patch("shared.email.send_overrun_warning_email") as warn_email:
            result = jobs_mod.mid_run_monitor_check(
                job_id, 7000.0, modal_client=modal,
            )
        assert result == "warned"
        warn_email.assert_called_once()
        modal.cancel.assert_not_called()
        assert fake_job_store[job_id]["status"] == "running"
        # Idempotency flag stashed so a second heartbeat does not re-email.
        assert (
            fake_job_store[job_id]["inputs"]["_wallet"]["overrun_warned"] is True
        )

    def test_runaway_cost_warns_only_once(self, fake_job_store):
        """The overrun_warned flag makes the (now unbounded) warning fire
        exactly once even as the ratio keeps climbing, so a persistently
        runaway job does not spam the user."""
        job_id = _seed_running(fake_job_store)
        fake_job_store[job_id]["inputs"]["num_designs"] = 2
        fake_job_store[job_id]["inputs"]["_wallet"]["estimate_usd"] = "0.05"
        # Already warned on a prior heartbeat.
        fake_job_store[job_id]["inputs"]["_wallet"]["overrun_warned"] = True
        with patch("shared.email.send_overrun_warning_email") as warn_email:
            result = jobs_mod.mid_run_monitor_check(job_id, 9000.0)
        warn_email.assert_not_called()
        assert result is None

    def test_warns_at_or_above_warn_ratio(self, fake_job_store):
        """At ~1.5x estimate (the warn threshold) the warning still fires;
        that telemetry is unaffected by removing the kill."""
        job_id = _seed_running(fake_job_store)
        # 4.40 estimate; ~5500 gpu_seconds -> ~1.5x.
        modal = MagicMock()
        with patch("shared.email.send_overrun_warning_email") as warn_email:
            result = jobs_mod.mid_run_monitor_check(
                job_id, 5500.0, modal_client=modal,
            )
        assert result == "warned"
        warn_email.assert_called_once()
        modal.cancel.assert_not_called()
        assert fake_job_store[job_id]["status"] == "running"


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


# ---------------------------------------------------------------------------
# _safe_gpu_seconds_int — heartbeat GPU-seconds sanitisation (REVIEW.md #17)
# ---------------------------------------------------------------------------


def test_safe_gpu_seconds_int_bounds_and_coerces():
    import math

    f = jobs_mod._safe_gpu_seconds_int
    # Normal values pass through as int.
    assert f(0) == 0
    assert f(12.7) == 12
    assert f("30") == 30
    # Non-finite / garbage / negative collapse to 0 rather than raising.
    assert f(float("nan")) == 0
    assert f(float("inf")) == 0
    assert f(-5) == 0
    assert f(None) == 0
    assert f("not-a-number") == 0
    # Absurd values are clamped to the 24h ceiling, never persisted raw.
    assert f(10**12) == jobs_mod._MAX_GPU_SECONDS
    assert f(math.inf) <= jobs_mod._MAX_GPU_SECONDS


# ---------------------------------------------------------------------------
# _stash_wallet_flag re-reads fresh inputs (REVIEW #16 follow-up)
# ---------------------------------------------------------------------------


def test_stash_wallet_flag_merges_onto_fresh_inputs(monkeypatch):
    # The `job` handed in is a stale snapshot from the top of the
    # heartbeat handler — it predates the heartbeat's _partial_candidates
    # append. The flag write must merge onto the CURRENT row, not the
    # snapshot, or it silently drops the concurrent heartbeat state.
    stale = ToolJob.from_row(_row(inputs={"_wallet": {}}))
    fresh = ToolJob.from_row(
        _row(
            id=stale.id,
            inputs={
                "_wallet": {},
                "_partial_candidates": [{"pdb_key": "a"}],
                "_hb_version": 3,
            },
        )
    )
    monkeypatch.setattr(jobs_mod, "get_job", lambda jid, **k: fresh)
    captured: dict = {}
    monkeypatch.setattr(
        jobs_mod, "update_inputs",
        lambda jid, inp: captured.update(inp) or True,
    )

    jobs_mod._stash_wallet_flag(stale, "overrun_warned", True)

    assert captured["_wallet"]["overrun_warned"] is True
    # Heartbeat state survives (would be dropped if it merged onto `stale`).
    assert captured["_partial_candidates"] == [{"pdb_key": "a"}]
    assert captured["_hb_version"] == 3


# ---------------------------------------------------------------------------
# JobRead's two guards, and the invariant underneath them
#
# JobRead is the oldest of the three read wrappers and shipped without either
# guard, under a docstring claiming "no `__bool__` and no truthiness of any
# kind": every instance was unconditionally truthy, and `frozen=True` GENERATED
# an `__eq__`, so `read == JOB_READ_OK` answered False in silence on a read that
# had succeeded. `shared.targets.TargetRead` and
# `shared.compute_campaigns.CampaignRead` copied the shape and the claim; all
# three were fixed together, because leaving one of them collapsible while all
# three assert the same property is the drift the class exists to stop.
#
# The precedent for both guards is `tools/proteina/_canary_scoring.py::Verdict`.
# ---------------------------------------------------------------------------


def _a_job(**over) -> ToolJob:
    """A ToolJob built from a row with FIXED ids.

    ``_row()`` mints a fresh uuid for ``id`` and ``user_id`` on every call, so
    two default jobs are never equal. Pinning both makes two calls return two
    DISTINCT objects that compare EQUAL, which is the only shape in which
    ``test_two_job_reads_still_compare_as_values`` can test anything: tuple
    ``==`` short-circuits on identity per element, so a second read built from
    the same job object never reaches ``ToolJob.__eq__`` at all.
    """
    return ToolJob.from_row(
        _row(**{"id": "j-fixed", "user_id": "u-fixed", **over})
    )


def test_a_job_read_refuses_to_be_used_as_a_boolean():
    """Asserted on the OK read FIRST, because that is where the default
    behaviour was most dangerous: `if read:` was True there and True on an
    unreadable one, so the natural spelling of "did this work" could not fail.

    No call site spells it that way today -- extending the guard to this class
    left the whole suite green, which is how that was established rather than
    assumed. The guard is what keeps the next one from being written.
    """
    from shared.jobs import (
        JOB_READ_ABSENT, JOB_READ_OK, JOB_READ_UNAVAILABLE, JobRead,
    )
    for read in (
        JobRead(_a_job(), JOB_READ_OK),
        JobRead(None, JOB_READ_ABSENT),
        JobRead(None, JOB_READ_UNAVAILABLE),
    ):
        with pytest.raises(TypeError):
            bool(read)
        with pytest.raises(TypeError):
            if read:            # noqa: SIM103 - the spelling under test
                pass
        with pytest.raises(TypeError):
            not read


def test_a_job_read_refuses_to_be_compared_with_an_outcome_string():
    """`__bool__` raising leaves a hole exactly its own size unless `__eq__`
    closes it too. Every route into `__eq__` is covered, because closing only
    the direct one leaves three spellings of the same error working."""
    from shared.jobs import JOB_READ_ABSENT, JOB_READ_OK, JobRead
    read = JobRead(_a_job(), JOB_READ_OK)
    assert read.outcome == JOB_READ_OK
    with pytest.raises(TypeError):
        read == JOB_READ_OK
    with pytest.raises(TypeError):
        JOB_READ_OK == read                 # the reflected comparison
    with pytest.raises(TypeError):
        read != JOB_READ_ABSENT             # `!=` routes through `__eq__`
    with pytest.raises(TypeError):
        read in (JOB_READ_OK, JOB_READ_ABSENT)          # and so does `in`
    # The cross-family mixup. THIS LINE IS DOCUMENTATION, NOT COVERAGE, and it
    # is written down here so nobody counts it as the latter: the guard tests
    # `isinstance(other, str)` and all three families spell OK as the same
    # interned `"ok"`, so no mutation can red this line without redding the
    # `read == JOB_READ_OK` assertion above it. Redefining CAMPAIGN_READ_OK to
    # another string leaves it green; narrowing the guard to
    # `other in JOB_READ_OUTCOMES` leaves it green too. Kept because both
    # sibling suites carry it and `JobRead`'s docstring claims it, so its
    # absence read as the claim being untested on the one class that makes it --
    # but the CONSTRUCTION half is what actually cannot be caught, and that is
    # register item A95's neighbourhood, not this test's.
    from shared.compute_campaigns import CAMPAIGN_READ_OK
    with pytest.raises(TypeError):
        read == CAMPAIGN_READ_OK


def test_two_job_reads_still_compare_as_values():
    """Refusing the string comparison must not cost ordinary equality, and it
    must not cost hashability either: declaring `__eq__` sets `__hash__` to
    None. ToolJob's own generated `__hash__` RAISES (three of its fields are
    dicts), so JobRead hashes the job's id instead.

    THE SECOND READ IS BUILT FROM A SECOND, INDEPENDENTLY CONSTRUCTED JOB, and
    that is the whole content of the test rather than a detail of it. `__eq__`
    compares `(self.job, self.outcome) == (other.job, other.outcome)`, and
    tuple `==` short-circuits on IDENTITY per element -- so two reads sharing
    one job object compare equal whether the payload comparison works or not,
    and this test passed unchanged against an `__eq__` rewritten to
    `self.job is other.job`. The precedent it copies is
    `tests/test_proteina_canary.py::test_two_verdicts_still_compare_as_values`,
    which builds its second Verdict with `metrics={"k": 1}` -- a distinct dict
    with equal contents -- for exactly this reason.
    """
    from shared.jobs import (
        JOB_READ_ABSENT, JOB_READ_OK, JOB_READ_UNAVAILABLE, JobRead,
    )
    job = _a_job()
    twin = _a_job()
    assert twin is not job, "the fixture must build a second object"
    assert twin == job, "and it must be equal to the first by value"
    a = JobRead(job, JOB_READ_OK)
    assert a == JobRead(twin, JOB_READ_OK)
    assert a != JobRead(None, JOB_READ_ABSENT)
    assert JobRead(None, JOB_READ_ABSENT) != JobRead(None, JOB_READ_UNAVAILABLE)
    # Two DIFFERENT jobs, so equality is not merely "same outcome".
    assert a != JobRead(_a_job(id="j-other"), JOB_READ_OK)
    assert len({a, JobRead(twin, JOB_READ_OK)}) == 1
    # The fact that makes the id-based hash necessary rather than a shortcut.
    with pytest.raises(TypeError):
        hash(job)
    # Not equal to some other type, and not raising either: only strings raise.
    assert a != 17


def test_reads_of_different_families_collide_in_hash_only():
    """THE SECOND AXIS THE GUARDS DO NOT COVER, pinned so the docstrings that
    claim it stay honest.

    All three ``__hash__`` implementations hash ``(payload id, outcome)`` and
    drop the class, so three reads from three families hash IDENTICALLY. That
    is legal and it is not an equality claim: ``__eq__`` returns
    ``NotImplemented`` for a foreign class, Python falls back to identity, and
    a set therefore keeps all three. Both halves are asserted, because the
    collision without the non-equality reads like a bug and the non-equality
    without the collision leaves the docstrings' admission unchecked.
    """
    from shared.compute_campaigns import CAMPAIGN_READ_ABSENT, CampaignRead
    from shared.jobs import JOB_READ_ABSENT, JobRead
    from shared.targets import TARGET_READ_ABSENT, TargetRead
    j = JobRead(None, JOB_READ_ABSENT)
    t = TargetRead(None, TARGET_READ_ABSENT)
    c = CampaignRead(None, CAMPAIGN_READ_ABSENT)
    assert hash(j) == hash(t) == hash(c)
    # And they are still three distinct values, so nothing is silently merged.
    assert (j == t) is False
    assert (t == c) is False
    assert len({j, t, c}) == 3


def test_a_job_read_that_is_ok_must_carry_a_job():
    from shared.jobs import JOB_READ_OK, JobRead
    with pytest.raises(ValueError):
        JobRead(None, JOB_READ_OK)


def test_a_job_read_that_is_not_ok_must_carry_no_job():
    from shared.jobs import JOB_READ_ABSENT, JOB_READ_UNAVAILABLE, JobRead
    job = _a_job()
    for outcome in (JOB_READ_ABSENT, JOB_READ_UNAVAILABLE):
        with pytest.raises(ValueError):
            JobRead(job, outcome)


def test_a_job_read_refuses_an_outcome_that_is_not_one_of_the_three():
    from shared.jobs import JobRead
    with pytest.raises(ValueError):
        JobRead(None, "unavailble")
