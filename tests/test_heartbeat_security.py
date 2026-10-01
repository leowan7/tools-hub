"""Security regression — the unauthenticated heartbeat cost path.

CSO audit 2026-06-17 H1: ``POST /webhooks/heartbeat`` carries no per-job
credential. The candidate-injection branch is token-gated, but the cost path
read ``cumulative_gpu_seconds`` straight from the request body and fed it to
``mid_run_monitor_check``. An attacker who learned a victim's running-job
UUID could POST ``{"job_id": "<uuid>", "cumulative_gpu_seconds": 999999}``
with no creds. The original exploit had two halves:

  1. CANCEL + billed kill. GONE, and not because of this fix: the 2.0x
     cost-based mid-run kill was removed in 3818b4a4 (PR #62).
     ``mid_run_monitor_check`` now returns only ``None`` or ``"warned"``
     (``shared/jobs.py``), so no heartbeat can cancel a Modal call or
     settle a ``safety_kill`` charge whatever the body says.
  2. INFLATED BILLING FIGURE. STILL LIVE, and this file is what guards it.
     ``mid_run_monitor_check`` persists its ``cumulative_gpu_seconds``
     argument to ``tool_jobs.gpu_seconds_used``, and that column is what a
     user-initiated cancel bills. A body-trusted figure would therefore
     still overcharge a victim, with no kill involved.

Half 2 is the whole remaining value of the fix, which makes the figure a
SERVER-SIDE wall-clock measurement (``_elapsed_running_seconds``) and
ignores the request-body value. Do not treat these tests as guarding a
removed feature and delete them: measured by mutating the fix to trust the
body again (2026-10-01), the only assertions that fail are the two on the
figure itself. Every assertion about cancel / status / failure_class passed
under that mutation and has been dropped.

These tests lock in:

  * a forged huge ``cumulative_gpu_seconds`` reaches the monitor as the
    server wall-clock value, never the body value;
  * a benign Kendrew heartbeat (no cost field) still drives the monitor
    off wall-clock;
  * the benign stage-string update still lands;
  * end-to-end, a forged heartbeat against a job with a live wallet hold
    persists no inflated consumed-GPU figure.

Fakes mirror ``tests/test_modal_webhook_finalize.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from webhooks import modal as modal_webhook

FORGED_SECONDS = 999_999


def _recent_iso(seconds_ago: float = 10.0) -> str:
    """An ISO started_at a few seconds in the past (small wall-clock)."""
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()


def _job(**over) -> SimpleNamespace:
    base = dict(
        id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        tool="boltzgen",
        preset="pilot",
        status="running",
        inputs={},
        result=None,
        error=None,
        modal_function_call_id="fc-stub-abc",
        job_token="t" * 64,
        gpu_seconds_used=None,
        created_at=_recent_iso(60),
        started_at=_recent_iso(10),
        completed_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _client():
    from flask import Flask

    app = Flask(__name__)
    modal_webhook.register_modal_webhooks(app)
    return app.test_client()


# ---------------------------------------------------------------------------
# Targeted — the value handed to the cost/kill monitor is server wall-clock
# ---------------------------------------------------------------------------


class TestCostPathIgnoresBodySeconds:
    def test_forged_huge_seconds_does_not_reach_monitor(self):
        """A forged ``cumulative_gpu_seconds`` must never be the figure the
        overrun monitor bills/kills on — only server wall-clock is."""
        job = _job(started_at=_recent_iso(10))
        expected = modal_webhook._elapsed_running_seconds(job)

        with patch.object(modal_webhook, "get_job", return_value=job), patch.object(
            modal_webhook, "_append_heartbeat_state"
        ), patch.object(modal_webhook, "_run_overrun_check") as spy:
            resp = _client().post(
                "/webhooks/heartbeat",
                json={
                    "job_id": job.id,
                    "stage": "designing",
                    "cumulative_gpu_seconds": FORGED_SECONDS,
                },
            )

        assert resp.status_code == 200
        spy.assert_called_once()
        passed_secs = spy.call_args[0][1]
        # The forged value must be discarded entirely...
        assert passed_secs != FORGED_SECONDS
        assert passed_secs < 600  # nowhere near the forged figure
        # ...and the wall-clock value used instead (generous tolerance for
        # the time spent inside the request).
        assert passed_secs == pytest.approx(expected, abs=5)

    def test_benign_heartbeat_without_cost_field_still_monitors(self):
        """Regression: real Kendrew heartbeats omit the cost field; the
        monitor must still run off server wall-clock."""
        job = _job(started_at=_recent_iso(20))
        expected = modal_webhook._elapsed_running_seconds(job)

        with patch.object(modal_webhook, "get_job", return_value=job), patch.object(
            modal_webhook, "_append_heartbeat_state"
        ), patch.object(modal_webhook, "_run_overrun_check") as spy:
            resp = _client().post(
                "/webhooks/heartbeat",
                json={"job_id": job.id, "stage": "folding"},
            )

        assert resp.status_code == 200
        spy.assert_called_once()
        passed_secs = spy.call_args[0][1]
        assert passed_secs == pytest.approx(expected, abs=5)
        assert passed_secs > 0


# ---------------------------------------------------------------------------
# End-to-end — forged heartbeat through the real monitor + fake Supabase
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "boltzgen",
        "preset": "pilot",
        "status": "running",
        "inputs": {},
        "result": None,
        "error": None,
        "modal_function_call_id": "fc-stub-boltzgen-pilot-abc",
        "job_token": "t" * 64,
        "gpu_seconds_used": None,
        "failure_class": None,
        "created_at": _recent_iso(60),
        "started_at": _recent_iso(10),
        "completed_at": None,
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
def patched_clients(store):
    """Point both shared.jobs and webhooks.modal at the fake store."""
    factory = _fake_client_factory(store)
    with patch.object(jobs_mod, "get_service_client", factory), patch.object(
        modal_webhook, "get_service_client", factory
    ):
        yield


class TestForgedHeartbeatCannotInflateTheBilledFigure:
    def test_forged_seconds_with_active_hold_persists_no_inflated_charge(
        self, patched_clients, store
    ):
        """The exploit payload: huge cumulative_gpu_seconds against a job
        that owns a live wallet hold. The figure persisted to
        ``gpu_seconds_used`` — what a later user-initiated cancel bills —
        must be the server wall-clock value (~10s), never the body value."""
        row = _row(
            status="running",
            started_at=_recent_iso(10),
            inputs={
                "_wallet": {
                    "hold_tx_id": "tx-123",
                    "estimate_usd": "0.50",
                    "gpu_class": "A100",
                }
            },
        )
        store.rows[row["id"]] = row

        # Patched only so _run_overrun_check builds a mock instead of a real
        # ModalClient. Nothing is asserted on it: the monitor has no cancel
        # step left to call (see the module docstring, half 1).
        with patch("gpu.modal_client.ModalClient"):
            resp = _client().post(
                "/webhooks/heartbeat",
                json={
                    "job_id": row["id"],
                    "stage": "designing",
                    "cumulative_gpu_seconds": FORGED_SECONDS,
                },
            )

        assert resp.status_code == 200

        stored = store.rows[row["id"]]
        # THE live assertion. Consumed GPU persisted must track wall-clock,
        # not the body: this column is what a user-initiated cancel bills, so
        # a body-trusted figure overcharges the victim. Mutating the handler
        # to trust the body reds exactly this line (86400 < 600).
        #
        # Deliberately not also asserting ``!= FORGED_SECONDS``: that form is
        # vacuous. ``shared/jobs.py::_safe_gpu_seconds_int`` clamps to
        # _MAX_GPU_SECONDS (24h), so the forged 999999 is rewritten to 86400
        # before it is persisted and the inequality holds even under the
        # exploit. Only the bound below distinguishes the two.
        assert (stored["gpu_seconds_used"] or 0) < 600

    def test_benign_stage_update_still_lands(self, patched_clients, store):
        """The legitimate use of the heartbeat — progress telemetry — keeps
        working: the stage string is persisted to inputs._progress."""
        row = _row(status="running", started_at=_recent_iso(8))
        store.rows[row["id"]] = row

        with patch("gpu.modal_client.ModalClient"):
            resp = _client().post(
                "/webhooks/heartbeat",
                json={
                    "job_id": row["id"],
                    "stage": "refolding",
                    "designs_completed": 7,
                    "designs_total": 20,
                    # even with a forged figure, the benign path is unaffected
                    "cumulative_gpu_seconds": FORGED_SECONDS,
                },
            )

        assert resp.status_code == 200
        progress = store.rows[row["id"]]["inputs"].get("_progress")
        assert progress == {
            "stage": "refolding",
            "designs_completed": 7,
            "designs_total": 20,
        }
        # And still no inflated consumed-GPU figure leaked through.
        assert store.rows[row["id"]]["gpu_seconds_used"] != FORGED_SECONDS
