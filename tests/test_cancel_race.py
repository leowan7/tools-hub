"""Phase 4 hardening — cancel vs. Modal webhook race.

Covers the CAS-style status guard added to ``shared.jobs``. Two code
paths can both transition a running job to a terminal status:

    1. User-initiated cancel via ``POST /jobs/<id>/cancel`` → ``cancel_job``
    2. Modal webhook arrival (COMPLETED / FAILED) → ``complete_job``

Before the guard, whichever UPDATE committed last won, which allowed:

  * A late webhook overwriting a user cancel (and the hold release still
    fired, yielding a "succeeded" row with the hold released — free GPU run).
  * A successful webhook being silently clobbered by a race-losing cancel,
    releasing the wallet hold for work that actually completed.
  * Concurrent cancels each passing a SELECT-status!='terminal' check and
    both releasing the hold — double release.

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

    Expected: row stays ``cancelled``, exactly one wallet hold release,
    ``complete_job`` short-circuits on the already-terminal row.
    """

    def test_late_webhook_is_noop_after_cancel(
        self, patched_service_client, store
    ):
        row = _row(
            status="running",
            inputs={"_wallet": {"hold_tx_id": "hold-race-1"}},
        )
        store.rows[row["id"]] = row
        fake_modal = MagicMock()
        fake_modal.cancel.return_value = {"ok": True, "error": None}

        # Stage 1: user cancel lands first. The submit-time wallet hold is
        # released and the row is marked cancelled — a cancelled run bills
        # nothing.
        with patch("shared.wallet.release_hold") as release:
            job_after_cancel, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )
        assert err is None
        assert job_after_cancel is not None
        assert job_after_cancel.status == "cancelled"
        assert release.call_count == 1
        assert release.call_args.args[0] == "hold-race-1"

        # Stage 2: late Modal webhook arrives with COMPLETED. complete_job
        # must be a no-op because the row is already terminal ('cancelled')
        # — no settle, no release, no success overwrite.
        with patch("shared.wallet.settle_hold") as settle_again, patch(
            "shared.wallet.release_hold"
        ) as release_again, patch.object(
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
        assert settle_again.call_count == 0
        assert release_again.call_count == 0
        # Row should still be the cancel payload: no succeeded overwrite.
        assert store.rows[row["id"]]["status"] == "cancelled"
        assert store.rows[row["id"]]["result"] is None


# ---------------------------------------------------------------------------
# 2. Webhook wins → subsequent cancel is refused, no credit change
# ---------------------------------------------------------------------------


class TestWebhookBeatsCancel:
    """Modal webhook lands COMPLETED first, then the user clicks cancel.

    Expected: ``cancel_job`` returns ``(None, 'already_succeeded')`` and
    does NOT touch the wallet (complete_job already settled the hold on
    the successful row).
    """

    def test_cancel_after_success_is_rejected(
        self, patched_service_client, store
    ):
        row = _row(status="running")
        store.rows[row["id"]] = row
        fake_modal = MagicMock()
        fake_modal.cancel.return_value = {"ok": True, "error": None}

        # Stage 1: webhook wins — complete_job transitions row to succeeded.
        with patch("shared.wallet.settle_hold") as success_settle, patch(
            "shared.wallet.release_hold"
        ), patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            fresh = jobs_mod.complete_job(
                row["id"],
                terminal_status="succeeded",
                result={"candidates": []},
            )
        assert fresh is not None
        assert fresh.status == "succeeded"
        # The row carries no wallet hold (empty inputs), so the settle
        # hook short-circuits. That is fine — this test is about the CAS
        # race, not the settle math.
        success_settle_count = success_settle.call_count

        # Stage 2: user clicks cancel. The ``cancel_job`` preflight sees
        # a terminal status and returns the already_succeeded error
        # without calling Modal or touching the wallet.
        with patch("shared.wallet.settle_hold") as cancel_settle, patch(
            "shared.wallet.release_hold"
        ) as cancel_release:
            job, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )
        assert job is None
        assert err == "already_succeeded"
        fake_modal.cancel.assert_not_called()
        assert cancel_settle.call_count == 0
        assert cancel_release.call_count == 0

        # Row state is unchanged — still succeeded.
        assert store.rows[row["id"]]["status"] == "succeeded"
        assert success_settle.call_count == success_settle_count


# ---------------------------------------------------------------------------
# 3. Direct CAS race — cancel's SELECT sees running, UPDATE loses to
#    an interleaved webhook. Refund MUST NOT fire.
# ---------------------------------------------------------------------------


class TestCancelCasLostRefundSkipped:
    """Simulates the actual race window: ``cancel_job`` reads status='running',
    then BEFORE ``mark_cancelled`` emits its UPDATE the webhook runs and
    flips the row to 'succeeded'. The cancel's CAS UPDATE returns 0 rows
    and the hold-release path must be skipped.
    """

    def test_refund_not_issued_when_cas_loses(
        self, patched_service_client, store
    ):
        row = _row(
            status="running",
            inputs={"_wallet": {"hold_tx_id": "hold-cas-loss"}},
        )
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

        with patch("shared.wallet.release_hold") as release, patch.object(
            jobs_mod, "mark_cancelled", racing_mark_cancelled
        ):
            job, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )

        # The cancel caller should see already_succeeded — and critically
        # the wallet hold must NOT be released (the winner owns settlement).
        assert job is None
        assert err == "already_succeeded"
        assert release.call_count == 0
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
