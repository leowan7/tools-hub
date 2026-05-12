"""Wire-up tests for ``_charge_workspace_for_completed_job`` and the
``create_job`` workspace-context stash.

Covers the integration point that links a tool_jobs row back to its
funding Workspace when the Modal webhook lands a terminal status:

* ``create_job(target_pdb_id=...)`` stashes context in ``inputs._workspace``.
* ``complete_job`` calls ``charge_for_job`` only when context is present
  and the job recorded real GPU time.
* Crossing the 80% cap warning threshold dispatches
  ``send_workspace_cap_warning``.
* Pipeline-reported ``gpu_sku`` in the result payload wins over the
  stashed-at-submission value.
* Failures of the workspace charge or warning email do not abort
  terminal-state finalisation (the legacy credits-ledger refund path
  still runs).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from shared.jobs import ToolJob
from shared.workspaces import Workspace


# ---------------------------------------------------------------------------
# Test fixtures (mirror tests/test_jobs_phase4.py)
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "rfdiffusion",
        "preset": "pilot",
        "status": "running",
        "inputs": {},
        "result": None,
        "error": None,
        "credits_cost": 22,
        "modal_function_call_id": "fc-stub-rfdiffusion-pilot-abc",
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
        self.inserts: list[dict] = []

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
    """Patch ``get_service_client`` so shared.jobs reads/writes from store.

    Mirrors the fake in tests/test_jobs_phase4.py — supports the SELECT,
    UPDATE, and INSERT shapes shared.jobs emits.
    """

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
                    return MagicMock(data=[])
                store.update(self._job_id, self._payload)
                return MagicMock(data=[dict(store.rows[self._job_id])])

        class _InsertQuery:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                row = dict(self._payload)
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault("status", "pending")
                row.setdefault("created_at", "2026-04-24T00:00:00Z")
                store.inserts.append(row)
                store.rows[row["id"]] = row
                return MagicMock(data=[dict(row)])

        table.select = lambda *_, **__: _SelectQuery()
        table.update = lambda payload: _UpdateQuery(payload)
        table.insert = lambda payload: _InsertQuery(payload)
        client.table.return_value = table
        return client

    with patch.object(jobs_mod, "get_service_client", _fake_client):
        yield


def _ws(
    *,
    user_id: str,
    target: str = "4Z18",
    cap: float = 100.0,
    spent: float = 0.0,
    sku: str = "workspace_standard",
    ws_id: str = "ws-fixture-1",
) -> Workspace:
    """Build a Workspace dataclass without touching Supabase."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return Workspace(
        id=ws_id,
        user_id=user_id,
        target_pdb_id=target,
        target_label=None,
        sku=sku,
        modal_cap_usd=cap,
        modal_spent_usd=spent,
        activated_at=now,
        expires_at=now + timedelta(days=30),
        refund_eligible_until=None,
        refunded_at=None,
        status="active",
        stripe_payment_intent_id=None,
        stripe_refund_id=None,
    )


# ---------------------------------------------------------------------------
# create_job: workspace context stash
# ---------------------------------------------------------------------------


class TestCreateJobWorkspaceContext:
    def test_target_pdb_id_stashed_in_inputs(
        self, patched_service_client, store
    ):
        original = {"existing_key": "value"}
        job = jobs_mod.create_job(
            user_id=str(uuid.uuid4()),
            tool="rfdiffusion",
            preset="pilot",
            inputs=original,
            credits_cost=10,
            target_pdb_id="4Z18",
        )
        assert job is not None
        assert job.inputs.get("_workspace", {}).get("target_pdb_id") == "4Z18"
        # Caller's dict not mutated.
        assert "_workspace" not in original
        assert original["existing_key"] == "value"

    def test_workspace_id_stashed_in_inputs(
        self, patched_service_client, store
    ):
        job = jobs_mod.create_job(
            user_id=str(uuid.uuid4()),
            tool="rfdiffusion",
            preset="pilot",
            inputs={},
            credits_cost=10,
            target_pdb_id="4Z18",
            workspace_id="ws-abc-123",
        )
        assert job is not None
        ws_ctx = job.inputs.get("_workspace") or {}
        assert ws_ctx.get("target_pdb_id") == "4Z18"
        assert ws_ctx.get("workspace_id") == "ws-abc-123"

    def test_no_target_no_workspace_key(
        self, patched_service_client, store
    ):
        """Backwards-compat: omit kwargs => no _workspace key added."""
        job = jobs_mod.create_job(
            user_id=str(uuid.uuid4()),
            tool="rfdiffusion",
            preset="pilot",
            inputs={"x": 1},
            credits_cost=10,
        )
        assert job is not None
        assert "_workspace" not in job.inputs

    def test_existing_workspace_context_merged(
        self, patched_service_client, store
    ):
        """If caller pre-set inputs._workspace.gpu_sku, target_pdb_id merges in."""
        job = jobs_mod.create_job(
            user_id=str(uuid.uuid4()),
            tool="rfdiffusion",
            preset="pilot",
            inputs={"_workspace": {"gpu_sku": "A100-80GB"}},
            credits_cost=10,
            target_pdb_id="4Z18",
        )
        assert job is not None
        ws_ctx = job.inputs.get("_workspace") or {}
        assert ws_ctx.get("gpu_sku") == "A100-80GB"
        assert ws_ctx.get("target_pdb_id") == "4Z18"


# ---------------------------------------------------------------------------
# _charge_workspace_for_completed_job: gating logic
# ---------------------------------------------------------------------------


class TestChargeGating:
    def _job(self, **over) -> ToolJob:
        return ToolJob.from_row(_row(**over))

    def test_skipped_when_no_workspace_context(self):
        """Legacy job without _workspace stash: charge_for_job not called."""
        job = self._job(
            status="succeeded", gpu_seconds_used=600, inputs={},
        )
        with patch("shared.workspaces.charge_for_job") as charge:
            jobs_mod._charge_workspace_for_completed_job(job)
        charge.assert_not_called()

    def test_skipped_when_status_not_terminal_pair(self):
        """Timeout/cancelled paths don't charge — GPU was either fully used
        (timeout) or never ran (cancelled handled elsewhere)."""
        for status in ("timeout", "cancelled", "running", "pending"):
            job = self._job(
                status=status, gpu_seconds_used=600,
                inputs={"_workspace": {"target_pdb_id": "4Z18"}},
            )
            with patch("shared.workspaces.charge_for_job") as charge:
                jobs_mod._charge_workspace_for_completed_job(job)
            charge.assert_not_called(), f"status={status} should not charge"

    def test_skipped_when_no_gpu_seconds(self):
        """gpu_seconds_used None / 0 => no compute consumed => no charge."""
        for gpu in (None, 0, -1):
            job = self._job(
                status="succeeded", gpu_seconds_used=gpu,
                inputs={"_workspace": {"target_pdb_id": "4Z18"}},
            )
            with patch("shared.workspaces.charge_for_job") as charge:
                jobs_mod._charge_workspace_for_completed_job(job)
            charge.assert_not_called(), f"gpu={gpu} should not charge"

    def test_skipped_when_no_active_workspace(self):
        """Orphan: target_pdb_id present but no active Workspace exists."""
        job = self._job(
            status="succeeded", gpu_seconds_used=600,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        with patch(
            "shared.workspaces.get_active_workspace", return_value=None
        ), patch("shared.workspaces.charge_for_job") as charge:
            jobs_mod._charge_workspace_for_completed_job(job)
        charge.assert_not_called()


# ---------------------------------------------------------------------------
# _charge_workspace_for_completed_job: happy path + threshold
# ---------------------------------------------------------------------------


class TestChargeAndWarn:
    def _job(self, **over) -> ToolJob:
        return ToolJob.from_row(_row(**over))

    def test_charge_called_with_correct_args(self):
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=600,
            tool="rfdiffusion",
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=0.0)
        ws_after = _ws(user_id=user_id, spent=0.6168)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ) as charge:
            jobs_mod._charge_workspace_for_completed_job(job)
        charge.assert_called_once()
        kwargs = charge.call_args.kwargs
        assert kwargs["gpu_seconds"] == 600
        assert kwargs["tool"] == "rfdiffusion"
        assert kwargs["job_id"] == job.id
        # gpu_sku unset => None passed (charge_for_job applies default rate)
        assert kwargs["gpu_sku"] is None

    def test_gpu_sku_from_result_payload_wins(self):
        """Pipeline reports the actual GPU it used in result — prefer that."""
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=600,
            inputs={"_workspace": {
                "target_pdb_id": "4Z18", "gpu_sku": "A10G",
            }},
            result={"gpu_sku": "H100", "candidates": []},
        )
        ws_before = _ws(user_id=user_id, spent=0.0)
        ws_after = _ws(user_id=user_id, spent=1.45)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ) as charge:
            jobs_mod._charge_workspace_for_completed_job(job)
        assert charge.call_args.kwargs["gpu_sku"] == "H100"

    def test_gpu_sku_falls_back_to_stash_when_no_result(self):
        """If pipeline didn't report gpu_sku, use the submission-time value."""
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=600,
            inputs={"_workspace": {
                "target_pdb_id": "4Z18", "gpu_sku": "L40S",
            }},
            result={"candidates": []},
        )
        ws_before = _ws(user_id=user_id, spent=0.0)
        ws_after = _ws(user_id=user_id, spent=0.36)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ) as charge:
            jobs_mod._charge_workspace_for_completed_job(job)
        assert charge.call_args.kwargs["gpu_sku"] == "L40S"

    def test_warning_email_sent_when_crossing_80pct(self):
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=600,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=70.0)   # 70%
        ws_after = _ws(user_id=user_id, spent=85.0)    # 85% (crossed 80)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ), patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="lab@example.com"
        ), patch("shared.email.send_workspace_cap_warning") as warn:
            jobs_mod._charge_workspace_for_completed_job(job)
        warn.assert_called_once()
        assert warn.call_args.kwargs["user_email"] == "lab@example.com"
        assert warn.call_args.kwargs["workspace"].id == ws_after.id

    def test_no_warning_when_not_crossing(self):
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=60,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=10.0)   # 10%
        ws_after = _ws(user_id=user_id, spent=11.0)    # 11% — well below 80
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ), patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="lab@example.com"
        ), patch("shared.email.send_workspace_cap_warning") as warn:
            jobs_mod._charge_workspace_for_completed_job(job)
        warn.assert_not_called()

    def test_no_warning_when_already_past_80pct(self):
        """Second charge that just nudges the bar from 85->90% should not
        re-fire the warning email — only the crossing event does."""
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=600,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=85.0)   # already past
        ws_after = _ws(user_id=user_id, spent=90.0)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ), patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="lab@example.com"
        ), patch("shared.email.send_workspace_cap_warning") as warn:
            jobs_mod._charge_workspace_for_completed_job(job)
        warn.assert_not_called()

    def test_failed_status_still_charges(self):
        """A failed run that consumed real GPU time still bills the cap."""
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="failed", gpu_seconds_used=400,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=0.0)
        ws_after = _ws(user_id=user_id, spent=0.41)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ) as charge:
            jobs_mod._charge_workspace_for_completed_job(job)
        charge.assert_called_once()

    def test_warning_email_failure_does_not_raise(self):
        """A flaky Resend POST must not abort terminal-state finalisation."""
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=600,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=70.0)
        ws_after = _ws(user_id=user_id, spent=85.0)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ), patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="lab@example.com"
        ), patch(
            "shared.email.send_workspace_cap_warning",
            side_effect=RuntimeError("Resend down"),
        ):
            # Must not raise.
            jobs_mod._charge_workspace_for_completed_job(job)

    def test_charge_failure_does_not_raise(self):
        """Workspaces module hiccup must not abort terminal-state finalisation."""
        user_id = str(uuid.uuid4())
        job = self._job(
            user_id=user_id, status="succeeded", gpu_seconds_used=600,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=0.0)
        with patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job",
            side_effect=RuntimeError("Supabase down"),
        ):
            # Must not raise.
            jobs_mod._charge_workspace_for_completed_job(job)


# ---------------------------------------------------------------------------
# complete_job integration: workspace charge fires after refund, before email
# ---------------------------------------------------------------------------


class TestCompleteJobIntegration:
    def _prime(self, store, **row_over):
        row = _row(**row_over)
        store.rows[row["id"]] = row
        return row

    def test_complete_job_invokes_workspace_charge(
        self, patched_service_client, store
    ):
        user_id = str(uuid.uuid4())
        row = self._prime(
            store, user_id=user_id,
            inputs={"_workspace": {"target_pdb_id": "4Z18"}},
        )
        ws_before = _ws(user_id=user_id, spent=0.0)
        ws_after = _ws(user_id=user_id, spent=0.6168)
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="lab@example.com"
        ), patch("shared.email.send_job_complete_email"), patch.object(
            jobs_mod, "_refund_unused_credits", lambda _job: None
        ), patch(
            "shared.workspaces.get_active_workspace", return_value=ws_before
        ), patch(
            "shared.workspaces.charge_for_job", return_value=ws_after
        ) as charge:
            jobs_mod.complete_job(
                row["id"],
                terminal_status="succeeded",
                result={"candidates": []},
                gpu_seconds_used=600,
            )
        charge.assert_called_once()
        assert charge.call_args.kwargs["gpu_seconds"] == 600

    def test_complete_job_no_workspace_context_no_charge(
        self, patched_service_client, store
    ):
        """Pre-Workspace job (legacy submit path) must not trigger charge."""
        row = self._prime(store, inputs={})
        with patch.object(
            jobs_mod, "_resolve_email_for_user", return_value="lab@example.com"
        ), patch("shared.email.send_job_complete_email"), patch.object(
            jobs_mod, "_refund_unused_credits", lambda _job: None
        ), patch("shared.workspaces.charge_for_job") as charge:
            jobs_mod.complete_job(
                row["id"],
                terminal_status="succeeded",
                result={"candidates": []},
                gpu_seconds_used=600,
            )
        charge.assert_not_called()
