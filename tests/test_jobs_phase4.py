"""Phase 4 — job completion, cancellation, pagination.

Verifies the three behaviours Phase 4 introduced:

1. ``complete_job`` calls ``send_job_complete_email`` for succeeded and
   failed terminal states, and skips it for timeout/cancelled.
2. ``cancel_job`` refunds the full credit cost, marks the row
   ``cancelled``, and is a no-op on already-terminal rows.
3. ``list_jobs_paginated`` returns (rows, total) with correct slicing.

Runs fully offline — ``shared.credits.get_service_client`` and
``shared.jobs._resolve_email_for_user`` are patched with a small
in-memory fake.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from shared.jobs import ToolJob


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    """Build a tool_jobs row dict with sensible defaults."""
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
    """Tiny row store with get/update semantics the job helpers exercise."""

    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.updates: list[tuple[str, dict]] = []

    def get(self, job_id: str) -> dict | None:
        r = self.rows.get(job_id)
        return dict(r) if r else None

    def update(self, job_id: str, payload: dict) -> None:
        self.rows[job_id].update(payload)
        self.updates.append((job_id, dict(payload)))


@pytest.fixture
def store():
    return _FakeJobsStore([])


@pytest.fixture
def patched_service_client(store):
    """Patch ``get_service_client`` so shared.jobs reads/writes from store."""

    def _fake_client():
        client = MagicMock()
        table = MagicMock()

        class _SelectQuery:
            def __init__(self):
                self._filters: dict = {}

            def eq(self, col, val):
                self._filters[col] = val
                return self

            def _matches(self, row):
                for k, v in self._filters.items():
                    if row.get(k) != v:
                        return False
                return True

            def single(self):
                return self

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
                self._allowed_statuses: list | None = None

            def eq(self, col, val):
                if col == "id":
                    self._job_id = val
                return self

            def in_(self, col, values):
                # CAS guard: only the row matching ``id`` AND whose
                # current status is in ``values`` is updated. Mirrors
                # PostgREST ``UPDATE ... WHERE status IN (...)``.
                if col == "status":
                    self._allowed_statuses = list(values)
                return self

            def execute(self):
                if self._job_id is None or self._job_id not in store.rows:
                    return MagicMock(data=[])
                current = store.rows[self._job_id].get("status")
                if (
                    self._allowed_statuses is not None
                    and current not in self._allowed_statuses
                ):
                    # CAS lost — do not mutate, return empty row list so
                    # ``_cas_update`` reports False.
                    return MagicMock(data=[])
                store.update(self._job_id, self._payload)
                return MagicMock(data=[dict(store.rows[self._job_id])])

        table.select = lambda *_, **__: _SelectQuery()
        table.update = lambda payload: _UpdateQuery(payload)
        client.table.return_value = table
        return client

    with patch.object(jobs_mod, "get_service_client", _fake_client):
        yield


# ---------------------------------------------------------------------------
# 1. Email fires on succeeded / failed, not on timeout / cancelled
# ---------------------------------------------------------------------------


class TestCompletionEmail:
    def _prime(self, store, **row_over):
        row = _row(**row_over)
        store.rows[row["id"]] = row
        return row

    def test_email_on_succeeded(self, patched_service_client, store):
        row = self._prime(store)
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="user@example.com"
        ), patch("shared.email.send_job_complete_email") as send, patch.object(
            jobs_mod, "_refund_unused_credits", lambda _job: None
        ):
            jobs_mod.complete_job(
                row["id"], terminal_status="succeeded", result={"candidates": []}
            )
        assert send.call_count == 1
        assert send.call_args.kwargs["user_email"] == "user@example.com"

    def test_email_on_failed(self, patched_service_client, store):
        row = self._prime(store)
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="user@example.com"
        ), patch("shared.email.send_job_complete_email") as send, patch.object(
            jobs_mod, "_refund_unused_credits", lambda _job: None
        ):
            jobs_mod.complete_job(
                row["id"],
                terminal_status="failed",
                error={"bucket": "pipeline", "detail": "x"},
            )
        assert send.call_count == 1

    def test_no_email_on_timeout(self, patched_service_client, store):
        row = self._prime(store)
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="user@example.com"
        ), patch("shared.email.send_job_complete_email") as send, patch.object(
            jobs_mod, "_refund_unused_credits", lambda _job: None
        ):
            jobs_mod.complete_job(row["id"], terminal_status="timeout")
        assert send.call_count == 0

    def test_no_email_when_replay(self, patched_service_client, store):
        # Already-terminal job should short-circuit — no refund, no email.
        row = self._prime(store, status="succeeded", completed_at="2026-04-24T00:00:00Z")
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="user@example.com"
        ), patch("shared.email.send_job_complete_email") as send, patch.object(
            jobs_mod, "_refund_unused_credits", lambda _job: None
        ):
            out = jobs_mod.complete_job(row["id"], terminal_status="succeeded")
        assert send.call_count == 0
        assert out is not None
        assert out.status == "succeeded"


# ---------------------------------------------------------------------------
# 1b. _refund_unused_credits: full refund on no-work failures, prorated on
#     under-budget successes, no refund when real GPU time was consumed.
# ---------------------------------------------------------------------------


class TestRefundUnusedCredits:
    def _job(self, **over) -> ToolJob:
        return ToolJob.from_row(_row(**over))

    def test_failed_with_no_gpu_time_full_refund(self):
        """Pipeline crashed before doing real work — refund full pre-auth."""
        job = self._job(
            status="failed", credits_cost=10, gpu_seconds_used=None, tool="boltzgen"
        )
        with patch("shared.credits.record_refund") as refund:
            jobs_mod._refund_unused_credits(job)
        refund.assert_called_once()
        assert refund.call_args.args[1] == 10
        assert refund.call_args.kwargs["tool"] == "boltzgen"
        assert refund.call_args.kwargs["metadata"]["refund_kind"] == "system_failure"

    def test_failed_with_zero_gpu_time_full_refund(self):
        """gpu_seconds_used=0 is the same signal as None — refund."""
        job = self._job(status="failed", credits_cost=15, gpu_seconds_used=0)
        with patch("shared.credits.record_refund") as refund:
            jobs_mod._refund_unused_credits(job)
        refund.assert_called_once()
        assert refund.call_args.args[1] == 15

    def test_failed_with_real_gpu_time_no_refund(self):
        """Real GPU consumed before failure — keep the credits."""
        job = self._job(status="failed", credits_cost=10, gpu_seconds_used=420)
        with patch("shared.credits.record_refund") as refund:
            jobs_mod._refund_unused_credits(job)
        refund.assert_not_called()

    def test_timeout_no_refund(self):
        """Timeout means GPU ran the full preset cap — no refund."""
        job = self._job(status="timeout", credits_cost=10, gpu_seconds_used=None)
        with patch("shared.credits.record_refund") as refund:
            jobs_mod._refund_unused_credits(job)
        refund.assert_not_called()

    def test_succeeded_under_cap_prorated_refund(self):
        """Used a quarter of the cap — refund three quarters."""
        job = self._job(
            status="succeeded", credits_cost=20, gpu_seconds_used=60, tool="bindcraft",
            preset="pilot",
        )
        with patch("shared.credits.record_refund") as refund, patch(
            "gpu.modal_client.preset_gpu_seconds", return_value=240
        ):
            jobs_mod._refund_unused_credits(job)
        refund.assert_called_once()
        # 60/240 used = 25% kept = 5 credits used, 15 refunded.
        assert refund.call_args.args[1] == 15

    def test_succeeded_no_gpu_time_no_refund(self):
        """No gpu_seconds_used recorded — cannot prorate, keep credits."""
        job = self._job(status="succeeded", credits_cost=20, gpu_seconds_used=None)
        with patch("shared.credits.record_refund") as refund:
            jobs_mod._refund_unused_credits(job)
        refund.assert_not_called()

    def test_zero_credits_cost_no_refund(self):
        """No pre-auth was debited (e.g. internal smoke tier) — nothing to refund."""
        job = self._job(status="failed", credits_cost=0, gpu_seconds_used=None)
        with patch("shared.credits.record_refund") as refund:
            jobs_mod._refund_unused_credits(job)
        refund.assert_not_called()


# ---------------------------------------------------------------------------
# 2. cancel_job refunds full credits + marks cancelled + idempotent
# ---------------------------------------------------------------------------


class TestCancelJob:
    def _prime(self, store, **row_over):
        row = _row(**row_over)
        store.rows[row["id"]] = row
        return row

    def test_cancel_running_job_refunds_and_marks(
        self, patched_service_client, store
    ):
        row = self._prime(store, status="running", credits_cost=22)
        fake_modal = MagicMock()
        fake_modal.cancel.return_value = {"ok": True, "error": None}
        with patch("shared.credits.record_refund") as refund:
            refund.return_value = True
            job, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )
        assert err is None
        assert job is not None
        assert job.status == "cancelled"
        fake_modal.cancel.assert_called_once_with(row["modal_function_call_id"])
        refund.assert_called_once()
        refund_kwargs = refund.call_args.kwargs
        assert refund_kwargs["tool"] == "bindcraft"
        assert refund.call_args.args[1] == 22  # full refund

    def test_cancel_already_terminal_is_refused(
        self, patched_service_client, store
    ):
        row = self._prime(store, status="succeeded")
        fake_modal = MagicMock()
        with patch("shared.credits.record_refund") as refund:
            job, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )
        assert job is None
        assert err == "already_succeeded"
        fake_modal.cancel.assert_not_called()
        refund.assert_not_called()

    def test_cancel_without_modal_fc_still_marks_cancelled(
        self, patched_service_client, store
    ):
        row = self._prime(store, modal_function_call_id=None)
        fake_modal = MagicMock()
        with patch("shared.credits.record_refund"):
            job, err = jobs_mod.cancel_job(
                row["id"], user_id=row["user_id"], modal_client=fake_modal
            )
        assert err is None
        assert job is not None
        assert job.status == "cancelled"
        fake_modal.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# 3. list_jobs_paginated slicing + total count
# ---------------------------------------------------------------------------


class TestListJobsPaginated:
    def test_returns_rows_and_count(self):
        user_id = str(uuid.uuid4())
        all_rows = [_row(user_id=user_id) for _ in range(57)]

        def _fake_client():
            client = MagicMock()
            table = MagicMock()

            class _Q:
                def __init__(self, rows):
                    self._rows = rows
                    self._start = 0
                    self._end = len(rows) - 1

                def eq(self, _col, _val):
                    return self

                def order(self, *_, **__):
                    return self

                def range(self, start, end):
                    self._start = start
                    self._end = end
                    return self

                def execute(self):
                    sliced = self._rows[self._start : self._end + 1]
                    return MagicMock(data=sliced, count=len(self._rows))

            def _select(*_, **kwargs):
                assert kwargs.get("count") == "exact"
                return _Q(all_rows)

            table.select = _select
            client.table.return_value = table
            return client

        with patch.object(jobs_mod, "get_service_client", _fake_client):
            rows, total = jobs_mod.list_jobs_paginated(
                user_id, page=2, page_size=25
            )
        assert total == 57
        assert len(rows) == 25
        # Page 3 returns the tail of 7 rows.
        with patch.object(jobs_mod, "get_service_client", _fake_client):
            rows, total = jobs_mod.list_jobs_paginated(
                user_id, page=3, page_size=25
            )
        assert len(rows) == 7

    def test_clamps_page_and_page_size(self):
        def _fake_client():
            client = MagicMock()
            table = MagicMock()

            def _select(*_, **__):
                q = MagicMock()
                q.eq.return_value = q
                q.order.return_value = q
                def _range(s, e):
                    _range.last = (s, e)
                    return q
                q.range = _range
                q.execute.return_value = MagicMock(data=[], count=0)
                q._ranger = _range
                return q

            table.select = _select
            client.table.return_value = table
            return client

        with patch.object(jobs_mod, "get_service_client", _fake_client):
            rows, total = jobs_mod.list_jobs_paginated(
                "u", page=0, page_size=9999
            )
        assert rows == []
        assert total == 0
