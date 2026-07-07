"""Lost-webhook recovery in the stuck-job sweeper.

Production incident 2026-07-06: the stuck-job sweeper fired at 17:04 during
the fund-and-drain canary and timed out a job that had genuinely SUCCEEDED.
Modal still held the payload and the designs were already in Storage, but
``timeout_stuck_job`` discarded the result and full-refunded the hold.

The fix makes ``timeout_stuck_job`` RECOVER such a job: before timing out it
probes Modal (inline ``FunctionCall.get``) and tool-outputs Storage, and when
the work survived it finalizes the job as ``succeeded`` through the same
``complete_job`` settle path the webhook uses (charge actual, release surplus)
rather than refunding a run that really executed.

Money invariant: recovery reuses the existing hold/settle machinery. A
recovered job settles via ``settle_hold`` (billed); a genuinely-dead job still
routes to ``release_hold`` (full refund). No parallel billing branch.

Fakes mirror ``tests/test_modal_webhook_finalize.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from shared.jobs import timeout_stuck_job


# ---------------------------------------------------------------------------
# Fake Supabase store with CAS-honouring update (copied shape from
# test_modal_webhook_finalize.py so terminal-state races behave like prod).
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "boltzgen",
        "preset": "pilot",
        "status": "running",
        "inputs": {"_wallet": {"hold_tx_id": "hold-1", "gpu_class": "a100-40gb"}},
        "result": None,
        "error": None,
        "modal_function_call_id": "fc-stub-boltzgen-pilot-abc",
        "job_token": "t" * 64,
        "gpu_seconds_used": None,
        "created_at": "2026-07-06T00:00:00Z",
        "started_at": "2026-07-06T00:00:00Z",
        "completed_at": None,
        "campaign_id": None,
        "failure_class": None,
    }
    base.update(over)
    return base


class _FakeJobsStore:
    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: dict(r) for r in rows}

    def update(self, job_id: str, payload: dict) -> None:
        self.rows[job_id].update(payload)


def _fake_client_factory(store: _FakeJobsStore):
    def _fake_client():
        client = MagicMock()
        table = MagicMock()

        class _SelectQuery:
            def __init__(self):
                self._filters: dict = {}

            def eq(self, col, val):
                self._filters[col] = val
                return self

            def single(self):
                return self

            def _matches(self, row):
                return all(row.get(k) == v for k, v in self._filters.items())

            def execute(self):
                rows = [r for r in store.rows.values() if self._matches(r)]
                return MagicMock(
                    data=(dict(rows[0]) if rows else None), count=len(rows)
                )

        class _UpdateQuery:
            def __init__(self, payload):
                self._payload = payload
                self._job_id = None
                self._allowed: list | None = None

            def eq(self, col, val):
                if col == "id":
                    self._job_id = val
                return self

            def in_(self, col, values):
                if col == "status":
                    self._allowed = list(values)
                return self

            def execute(self):
                if self._job_id is None or self._job_id not in store.rows:
                    return MagicMock(data=[])
                current = store.rows[self._job_id].get("status")
                if self._allowed is not None and current not in self._allowed:
                    return MagicMock(data=[])
                store.update(self._job_id, self._payload)
                return MagicMock(data=[dict(store.rows[self._job_id])])

        table.select = lambda *_, **__: _SelectQuery()
        table.update = lambda payload: _UpdateQuery(payload)
        client.table.return_value = table
        return client

    return _fake_client


@pytest.fixture
def store():
    return _FakeJobsStore([])


@pytest.fixture
def patched_service_client(store):
    with patch.object(jobs_mod, "get_service_client", _fake_client_factory(store)):
        yield


# ---------------------------------------------------------------------------
# Recovery: lost webhook but designs are in Storage -> finalized succeeded.
# ---------------------------------------------------------------------------


class TestRecoverFromStorage:
    def test_lost_webhook_succeeded_job_is_recovered_and_billed(
        self, patched_service_client, store
    ):
        # Modal unreachable (offline stub id) but heartbeats show every
        # design finished -> the _progress completion signal authorises the
        # Storage recovery.
        partials = [
            {"rank": 1, "pdb_key": "design_001.cif", "iptm": 0.81, "plddt": 90.1},
            {"rank": 2, "pdb_key": "design_002.cif", "iptm": 0.77, "plddt": 88.4},
        ]
        row = _row(
            status="running",
            gpu_seconds_used=6000,  # heartbeat-persisted actual consumption
            inputs={
                "_wallet": {"hold_tx_id": "hold-1", "gpu_class": "a100-40gb"},
                "_partial_candidates": partials,
                "_progress": {"designs_completed": 2, "designs_total": 2},
            },
        )
        store.rows[row["id"]] = row

        with patch("shared.storage.output_exists", return_value=True), patch(
            "shared.wallet.settle_hold"
        ) as settle, patch("shared.wallet.release_hold") as release, patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            outcome = timeout_stuck_job(row["id"])

        assert outcome == "recovered"
        stored = store.rows[row["id"]]
        assert stored["status"] == "succeeded"
        assert stored["result"]["backfilled"] is True
        assert stored["result"]["candidate_count"] == 2
        assert stored["failure_class"] == "succeeded"
        # Billed path: settle against actual GPU, NOT a full refund.
        settle.assert_called_once()
        assert settle.call_args.kwargs["gpu_seconds"] == pytest.approx(6000.0)
        release.assert_not_called()

    def test_storage_holds_designs_without_partials(
        self, patched_service_client, store
    ):
        # No streamed partials — reconstruct falls back to a Storage listing.
        # _progress confirms full completion so the listing is trustworthy.
        row = _row(
            status="running",
            gpu_seconds_used=1200,
            inputs={
                "_wallet": {"hold_tx_id": "hold-1", "gpu_class": "a100-40gb"},
                "_progress": {"designs_completed": 3, "designs_total": 3},
            },
        )
        store.rows[row["id"]] = row

        with patch(
            "shared.job_recovery._list_design_files",
            return_value=["design_001.cif", "design_002.cif", "design_003.cif"],
        ), patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release, patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            outcome = timeout_stuck_job(row["id"])

        assert outcome == "recovered"
        assert store.rows[row["id"]]["status"] == "succeeded"
        assert store.rows[row["id"]]["result"]["candidate_count"] == 3
        settle.assert_called_once()
        release.assert_not_called()


# ---------------------------------------------------------------------------
# Recovery via Modal inline result (atomic tools / inline-return pipelines).
# ---------------------------------------------------------------------------


class TestRecoverFromModalPoll:
    def test_modal_inline_result_is_recovered(
        self, patched_service_client, store
    ):
        row = _row(
            tool="mpnn",
            preset="standalone",
            status="running",
            gpu_seconds_used=None,
            modal_function_call_id="fc-real-123",  # non-stub -> poll is attempted
            inputs={"_wallet": {"hold_tx_id": "hold-1", "gpu_class": "cpu"}},
        )
        store.rows[row["id"]] = row

        poll_return = {
            "status": "succeeded",
            "result": {"sequences": ["MKTAYIAK", "MKTAYIAL"]},
            "gpu_seconds_used": 300,
            "error": None,
        }
        with patch(
            "gpu.modal_client.ModalClient.poll", return_value=poll_return
        ), patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release, patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            outcome = timeout_stuck_job(row["id"])

        assert outcome == "recovered"
        stored = store.rows[row["id"]]
        assert stored["status"] == "succeeded"
        assert stored["result"]["sequences"] == ["MKTAYIAK", "MKTAYIAL"]
        # Runtime carried by the poll flows through to the settle.
        settle.assert_called_once()
        assert settle.call_args.kwargs["gpu_seconds"] == pytest.approx(300.0)
        release.assert_not_called()

    def test_clean_exit_zero_webhook_lost_recovers_from_storage(
        self, patched_service_client, store
    ):
        # Composite pilots poll as 'failed' (webhook path, no inline payload).
        # A pipeline EXIT CODE of 0 proves the run finished and only the
        # callback was lost -> Storage recovery is authorised even without a
        # _progress snapshot.
        partials = [{"rank": 1, "pdb_key": "design_001.cif", "iptm": 0.8}]
        row = _row(
            status="running",
            gpu_seconds_used=6000,
            modal_function_call_id="fc-real-abc",
            inputs={
                "_wallet": {"hold_tx_id": "hold-1", "gpu_class": "a100-40gb"},
                "_partial_candidates": partials,
            },
        )
        store.rows[row["id"]] = row

        clean_exit_poll = {
            "status": "failed",
            "result": None,
            "exit_code": 0,
            "error": "webhook delivery failed (pipeline exited 0)",
        }
        with patch(
            "gpu.modal_client.ModalClient.poll", return_value=clean_exit_poll
        ), patch("shared.storage.output_exists", return_value=True), patch(
            "shared.wallet.settle_hold"
        ) as settle, patch("shared.wallet.release_hold") as release, patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            outcome = timeout_stuck_job(row["id"])

        assert outcome == "recovered"
        assert store.rows[row["id"]]["status"] == "succeeded"
        settle.assert_called_once()
        release.assert_not_called()


# ---------------------------------------------------------------------------
# Genuinely-dead job: nothing recoverable -> timeout + full refund.
# ---------------------------------------------------------------------------


class TestGenuinelyDeadJobStillTimesOut:
    def test_no_result_anywhere_times_out_and_refunds(
        self, patched_service_client, store
    ):
        row = _row(status="running", gpu_seconds_used=None)
        store.rows[row["id"]] = row

        with patch(
            "shared.job_recovery._list_design_files", return_value=[]
        ), patch("shared.storage.output_exists", return_value=False), patch(
            "shared.wallet.settle_hold"
        ) as settle, patch("shared.wallet.release_hold") as release:
            outcome = timeout_stuck_job(row["id"])

        assert outcome == "timed_out"
        stored = store.rows[row["id"]]
        assert stored["status"] == "timeout"
        assert stored["failure_class"] == "no_progress_timeout"
        # Refund path: release the hold, never settle a charge.
        release.assert_called_once()
        settle.assert_not_called()

    def test_crashed_pipeline_with_partial_designs_is_not_billed(
        self, patched_service_client, store
    ):
        # The inverse of the bug: a run that streamed a few designs then
        # GENUINELY crashed (nonzero pipeline exit), whose FAILED webhook was
        # ALSO lost. Storage holds the partial designs, but a nonzero exit is
        # ground-truth failure -> the sweeper must time it out and REFUND, not
        # bill it as a success.
        partials = [
            {"rank": 1, "pdb_key": "design_001.cif", "iptm": 0.7},
            {"rank": 2, "pdb_key": "design_002.cif", "iptm": 0.6},
        ]
        row = _row(
            status="running",
            gpu_seconds_used=3000,
            modal_function_call_id="fc-real-crash",
            inputs={
                "_wallet": {"hold_tx_id": "hold-1", "gpu_class": "a100-40gb"},
                "_partial_candidates": partials,
                # Partial progress: 2 of 50 designs before the crash.
                "_progress": {"designs_completed": 2, "designs_total": 50},
            },
        )
        store.rows[row["id"]] = row

        crash_poll = {
            "status": "failed",
            "result": None,
            "exit_code": 1,
            "error": "run_pipeline exited 1 with no smoke_result",
        }
        with patch(
            "gpu.modal_client.ModalClient.poll", return_value=crash_poll
        ), patch("shared.storage.output_exists", return_value=True), patch(
            "shared.wallet.settle_hold"
        ) as settle, patch("shared.wallet.release_hold") as release:
            outcome = timeout_stuck_job(row["id"])

        assert outcome == "timed_out"
        assert store.rows[row["id"]]["status"] == "timeout"
        release.assert_called_once()
        settle.assert_not_called()

    def test_incomplete_progress_without_ground_truth_times_out(
        self, patched_service_client, store
    ):
        # Modal unreachable (offline stub) and heartbeats show the run never
        # finished all designs. Without positive completion evidence we must
        # NOT resurrect the partial Storage designs -> timeout + refund.
        partials = [{"rank": 1, "pdb_key": "design_001.cif", "iptm": 0.7}]
        row = _row(
            status="running",
            gpu_seconds_used=3000,
            inputs={
                "_wallet": {"hold_tx_id": "hold-1", "gpu_class": "a100-40gb"},
                "_partial_candidates": partials,
                "_progress": {"designs_completed": 1, "designs_total": 50},
            },
        )
        store.rows[row["id"]] = row

        with patch("shared.storage.output_exists", return_value=True), patch(
            "shared.wallet.settle_hold"
        ) as settle, patch("shared.wallet.release_hold") as release:
            outcome = timeout_stuck_job(row["id"])

        assert outcome == "timed_out"
        assert store.rows[row["id"]]["status"] == "timeout"
        release.assert_called_once()
        settle.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency / race: a webhook that terminalises first must not be
# double-settled by the sweeper.
# ---------------------------------------------------------------------------


class TestWebhookWinsTheRace:
    def test_already_succeeded_job_is_a_noop(
        self, patched_service_client, store
    ):
        # Webhook already finalized + settled the job before the sweep ran.
        row = _row(
            status="succeeded",
            failure_class="succeeded",
            result={"candidates": [{"rank": 1, "pdb_key": "designs/d1.cif"}]},
            gpu_seconds_used=6000,
        )
        store.rows[row["id"]] = row

        with patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release, patch(
            "shared.job_recovery.recover_stuck_job_result"
        ) as recover:
            outcome = timeout_stuck_job(row["id"])

        assert outcome == ""
        # No recovery probe, no settle, no refund — the winner owns it.
        recover.assert_not_called()
        settle.assert_not_called()
        release.assert_not_called()
        assert store.rows[row["id"]]["status"] == "succeeded"

    def test_cas_lost_mid_recovery_does_not_double_settle(
        self, patched_service_client, store
    ):
        # The row is 'running' when the sweeper reads it and recovery finds a
        # result, but a concurrent writer terminalises it before our CAS
        # lands (simulated by forcing every CAS update to no-op). The sweeper
        # must NOT settle — the winning writer owns the wallet.
        partials = [{"rank": 1, "pdb_key": "design_001.cif", "iptm": 0.8}]
        row = _row(
            status="running",
            gpu_seconds_used=6000,
            inputs={
                "_wallet": {"hold_tx_id": "hold-1", "gpu_class": "a100-40gb"},
                "_partial_candidates": partials,
                # Full completion so recovery enters the complete_job path
                # where the CAS is then lost.
                "_progress": {"designs_completed": 1, "designs_total": 1},
            },
        )
        store.rows[row["id"]] = row

        with patch("shared.storage.output_exists", return_value=True), patch.object(
            jobs_mod, "_cas_update", return_value=False
        ), patch("shared.wallet.settle_hold") as settle, patch(
            "shared.wallet.release_hold"
        ) as release, patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            outcome = timeout_stuck_job(row["id"])

        assert outcome == ""
        settle.assert_not_called()
        release.assert_not_called()
        # Row never moved off 'running' (CAS no-op'd); a later sweep retries.
        assert store.rows[row["id"]]["status"] == "running"
