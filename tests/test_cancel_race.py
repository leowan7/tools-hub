"""Phase 4 hardening — cancel vs. Modal webhook race.

Covers the CAS-style status guard added to ``shared.jobs``. Two code
paths can both transition a running job to a terminal status:

    1. User-initiated cancel via ``POST /jobs/<id>/cancel`` → ``cancel_job``
    2. Modal webhook arrival (COMPLETED / FAILED) → ``complete_job``

Before the guard, whichever UPDATE committed last won, which allowed:

  * A late webhook overwriting a user cancel (and the refund still fired,
    yielding a "succeeded" row with the user refunded — free GPU run).
  * A successful webhook being silently clobbered by a race-losing cancel,
    handing the user a refund for work that actually completed.
  * Concurrent cancels each passing a SELECT-status!='terminal' check and
    both issuing refunds — double refund.

These tests simulate each race by interleaving calls against the same
in-memory fake store the rest of Phase 4 uses.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from webhooks import modal as modal_webhook


# ---------------------------------------------------------------------------
# Fakes — a trimmed copy of the fixture pattern in test_jobs_phase4.py.
# Kept local so this file is readable in isolation.
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


class _FakeJobsStore:
    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.updates: list[tuple[str, dict]] = []

    def update(self, job_id: str, payload: dict) -> None:
        self.rows[job_id].update(payload)
        self.updates.append((job_id, dict(payload)))


def _fake_client_factory(store: _FakeJobsStore):
    """Return a zero-arg factory that yields a fresh MagicMock-backed
    client over ``store``. Supports eq / in_ / single / update semantics."""

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
                for k, v in self._filters.items():
                    if row.get(k) != v:
                        return False
                return True

            def execute(self):
                rows = [r for r in store.rows.values() if self._matches(r)]
                return MagicMock(
                    data=(dict(rows[0]) if rows else None),
                    count=len(rows),
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
    fake = _fake_client_factory(store)
    with patch.object(jobs_mod, "get_service_client", fake):
        yield


# ---------------------------------------------------------------------------
# 1. Cancel wins → late webhook is a no-op
# ---------------------------------------------------------------------------


class TestCancelBeatsLateWebhook:
    """User cancels, THEN a late Modal webhook arrives with COMPLETED.

    Expected: row stays ``cancelled``, exactly one refund on the ledger,
    ``complete_job`` reports CAS loss → webhook handler returns
    ``already_terminal``.
    """

    def test_late_webhook_is_noop_after_cancel(
        self, patched_service_client, store
    ):
        row = _row(status="running", credits_cost=22)
        store.rows[row["id"]] = row
        fake_modal = MagicMock()
        fake_modal.cancel.return_value = {"ok": True, "error": None}

        # Stage 1: user cancel lands first. Full refund + row cancelled.
        # ``get_spent_for_job`` is patched to mirror the production case
        # where ``record_spend`` already debited the user — cancel refunds
        # what the ledger reports, not the row's ``credits_cost`` field.
        with patch("shared.credits.record_refund") as refund, patch(
            "shared.credits.get_spent_for_job", return_value=22
        ):
            refund.return_value = True
            job_after_cancel, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )
        assert err is None
        assert job_after_cancel is not None
        assert job_after_cancel.status == "cancelled"
        cancel_refund_calls = refund.call_count
        assert cancel_refund_calls == 1
        assert refund.call_args.args[1] == 22  # full-cost refund

        # Stage 2: late Modal webhook arrives with COMPLETED. complete_job
        # must be a no-op because the CAS guard rejects a transition out
        # of 'cancelled'. No second refund, no success overwrite.
        with patch("shared.credits.record_refund") as refund_again, patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            fresh = jobs_mod.complete_job(
                row["id"],
                terminal_status="succeeded",
                result={"candidates": [], "runtime_seconds": 300},
                gpu_seconds_used=300,
            )
        assert fresh is not None
        assert fresh.status == "cancelled"
        assert refund_again.call_count == 0
        # Row should still be the cancel payload: no succeeded overwrite.
        assert store.rows[row["id"]]["status"] == "cancelled"
        assert store.rows[row["id"]]["result"] is None


# ---------------------------------------------------------------------------
# 2. Webhook wins → subsequent cancel is refused, no credit change
# ---------------------------------------------------------------------------


class TestWebhookBeatsCancel:
    """Modal webhook lands COMPLETED first, then the user clicks cancel.

    Expected: ``cancel_job`` returns ``(None, 'already_succeeded')`` and
    does NOT issue a refund (complete_job already ran the prorated
    refund path on the successful row).
    """

    def test_cancel_after_success_is_rejected(
        self, patched_service_client, store
    ):
        row = _row(status="running", credits_cost=22)
        store.rows[row["id"]] = row
        fake_modal = MagicMock()
        fake_modal.cancel.return_value = {"ok": True, "error": None}

        # Stage 1: webhook wins — complete_job transitions row to succeeded.
        with patch("shared.credits.record_refund") as success_refund, patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            fresh = jobs_mod.complete_job(
                row["id"],
                terminal_status="succeeded",
                result={"candidates": []},
            )
        assert fresh is not None
        assert fresh.status == "succeeded"
        # No prorated refund in this fixture because gpu_seconds_used is
        # still None on the row (we passed no runtime_seconds), so the
        # _refund_unused_credits short-circuits. That is fine — the test
        # is about the CAS race, not about the prorated refund math.
        success_refund_count = success_refund.call_count

        # Stage 2: user clicks cancel. The ``cancel_job`` preflight sees
        # a terminal status and returns the already_succeeded error
        # without calling Modal or writing a refund.
        with patch("shared.credits.record_refund") as cancel_refund:
            job, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )
        assert job is None
        assert err == "already_succeeded"
        fake_modal.cancel.assert_not_called()
        assert cancel_refund.call_count == 0

        # Row state is unchanged — still succeeded, no new ledger writes
        # beyond whatever complete_job produced in stage 1.
        assert store.rows[row["id"]]["status"] == "succeeded"
        assert success_refund.call_count == success_refund_count


# ---------------------------------------------------------------------------
# 3. Direct CAS race — cancel's SELECT sees running, UPDATE loses to
#    an interleaved webhook. Refund MUST NOT fire.
# ---------------------------------------------------------------------------


class TestCancelCasLostRefundSkipped:
    """Simulates the actual race window: ``cancel_job`` reads status='running',
    then BEFORE ``mark_cancelled`` emits its UPDATE the webhook runs and
    flips the row to 'succeeded'. The cancel's CAS UPDATE returns 0 rows
    and the refund path must be skipped.
    """

    def test_refund_not_issued_when_cas_loses(
        self, patched_service_client, store
    ):
        row = _row(status="running", credits_cost=22)
        store.rows[row["id"]] = row
        fake_modal = MagicMock()
        fake_modal.cancel.return_value = {"ok": True, "error": None}

        # Wedge the race: after cancel_job's SELECT (get_job) sees the
        # row as running, flip it to 'succeeded' right before the CAS
        # UPDATE fires. Easiest hook point is mark_cancelled, which is
        # called after the SELECT but is where the UPDATE actually happens.
        real_mark_cancelled = jobs_mod.mark_cancelled

        def racing_mark_cancelled(job_id, **kwargs):
            # Simulate the webhook landing right now: terminalise the row
            # in the store, THEN let the real CAS UPDATE run. The CAS
            # UPDATE will see status='succeeded' and match zero rows.
            store.rows[job_id]["status"] = "succeeded"
            return real_mark_cancelled(job_id, **kwargs)

        with patch("shared.credits.record_refund") as refund, patch.object(
            jobs_mod, "mark_cancelled", racing_mark_cancelled
        ):
            job, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )

        # The cancel caller should see already_succeeded — and critically
        # no refund should have been recorded.
        assert job is None
        assert err == "already_succeeded"
        assert refund.call_count == 0
        assert store.rows[row["id"]]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# 4. Webhook endpoint surface — late POST returns already_terminal
# ---------------------------------------------------------------------------


class TestWebhookHandlerAlreadyTerminal:
    """End-to-end check at the Flask route layer: a late Modal webhook
    against a cancelled job returns ``{"status": "already_terminal"}`` with
    a 200 and does not mutate state."""

    def test_late_webhook_returns_already_terminal(
        self, patched_service_client, store
    ):
        from flask import Flask
        row = _row(status="cancelled", credits_cost=22)
        store.rows[row["id"]] = row

        app = Flask(__name__)
        modal_webhook.register_modal_webhooks(app)
        client = app.test_client()

        resp = client.post(
            f"/webhooks/modal/{row['id']}/{row['job_token']}",
            json={"status": "COMPLETED", "output": {"candidates": []}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "already_terminal"
        assert body["current"] == "cancelled"
        # Store unchanged.
        assert store.rows[row["id"]]["status"] == "cancelled"
        assert store.rows[row["id"]]["result"] is None
