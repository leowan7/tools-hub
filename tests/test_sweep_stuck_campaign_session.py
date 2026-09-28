"""The running sweep must not refund a campaign piece inside its session budget.

A bindcraft campaign piece is 16 designs at 1800 GPU-s each (~8 h) and is sent
to Modal with a 10 h ``_total_budget_hours``; the flat running cutoff is 6 h.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cron import sweep_stuck_jobs as sweep_mod
from shared.compute_campaigns import plan_chunks

pytestmark = pytest.mark.usefixtures("isolate_supabase")


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self._status = None

    def select(self, *_a):
        return self

    def eq(self, col, val):
        if col == "status":
            self._status = val
        return self

    def lt(self, *_a):
        return self

    def execute(self):
        rows = [r for r in self._rows if r["status"] == self._status]
        return type("R", (), {"data": rows})()


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _Query(self._rows)


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_a_campaign_bindcraft_piece_is_not_swept_before_its_budget(monkeypatch):
    plan = plan_chunks("bindcraft", 100)
    assert plan.chunk_size * 1800 > 6 * 3600  # the piece outlives the flat cutoff

    rows = [
        {"id": "piece-7h", "status": "running", "tool": "bindcraft",
         "campaign_id": "c1", "started_at": _ago(7)},
        {"id": "piece-13h", "status": "running", "tool": "bindcraft",
         "campaign_id": "c1", "started_at": _ago(13)},
        {"id": "single-7h", "status": "running", "tool": "bindcraft",
         "campaign_id": None, "started_at": _ago(7)},
    ]
    swept: list[str] = []
    monkeypatch.setattr("shared.credits.get_service_client", lambda: _Client(rows))
    monkeypatch.setattr(
        "shared.jobs.timeout_stuck_job",
        lambda job_id, **_k: swept.append(job_id) or "timed_out",
    )

    summary = sweep_mod.sweep_stuck_jobs(pending_age_minutes=30, running_age_hours=6)

    assert swept == ["piece-13h", "single-7h"]
    assert summary["running_swept"] == 2
