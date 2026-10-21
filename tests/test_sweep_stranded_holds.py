"""The stuck-job sweep retries holds that a failed release or settle left open.

``shared.wallet.release_hold`` logs a failure and returns False, and its
no caller retries it, so the sweep is the retry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cron import sweep_stuck_jobs as sweep_mod

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _field(row, col):
    if col == "inputs->_wallet->>hold_tx_id":
        return str(((row.get("inputs") or {}).get("_wallet") or {}).get("hold_tx_id"))
    return row.get(col)


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self._tests = []

    def select(self, *_a):
        return self

    def order(self, *_a):
        return self

    def range(self, *_a):
        return self

    def eq(self, col, val):
        self._tests.append(lambda r: _field(r, col) == val)
        return self

    def neq(self, col, val):
        self._tests.append(lambda r: _field(r, col) != val)
        return self

    def lt(self, col, val):
        self._tests.append(lambda r: str(_field(r, col)) < val)
        return self

    def gte(self, col, val):
        self._tests.append(lambda r: str(_field(r, col)) >= val)
        return self

    def in_(self, col, vals):
        self._tests.append(lambda r: str(_field(r, col)) in vals)
        return self

    def execute(self):
        rows = [r for r in self._rows if all(t(r) for t in self._tests)]
        return type("R", (), {"data": rows})()


class _Client:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables[name])


def test_open_holds_are_released_or_resettled_by_job_state(monkeypatch):
    ledger = [
        {"id": 1, "kind": "hold", "created_at": _ago(3)},  # no job: release
        {"id": 2, "kind": "hold", "created_at": _ago(3)},  # job failed: settle
        {"id": 3, "kind": "hold", "created_at": _ago(3)},  # job running: leave
        {"id": 4, "kind": "hold", "created_at": _ago(3)},  # released already
        {"id": 5, "kind": "hold_release", "created_at": _ago(3), "parent_tx_id": 4},
        {"id": 6, "kind": "hold", "created_at": _ago(1)},  # too young
        {"id": 7, "kind": "hold", "created_at": _ago(3)},  # live, debited only
        {"id": 8, "kind": "run_debit", "created_at": _ago(3), "parent_tx_id": 7},
    ]
    jobs = [
        {"id": "j2", "status": "failed", "inputs": {"_wallet": {"hold_tx_id": "2"}}},
        {"id": "j3", "status": "running", "started_at": _ago(1),
         "inputs": {"_wallet": {"hold_tx_id": "3"}}},
        {"id": "j7", "status": "succeeded", "inputs": {"_wallet": {"hold_tx_id": "7"}}},
    ]
    client = _Client({"wallet_transactions": ledger, "tool_jobs": jobs})
    released, settled = [], []
    monkeypatch.setattr("shared.credits.get_service_client", lambda: client)
    monkeypatch.setattr(
        "shared.wallet.release_hold",
        lambda hold_id, reason=None: released.append(hold_id) or True,
    )
    monkeypatch.setattr(
        "shared.jobs._settle_wallet_hold_for_completed_job",
        lambda job: settled.append(job.id),
    )
    monkeypatch.setattr("shared.jobs.ToolJob.from_row", classmethod(
        lambda cls, row: type("J", (), {"id": row["id"]})()
    ))

    summary = sweep_mod.sweep_stuck_jobs()

    assert released == ["1"]
    assert settled == ["j2", "j7"]
    assert summary["holds_retried"] == 3
    assert summary["errors"] == []
