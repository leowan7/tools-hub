"""The running sweep must not refund a campaign piece inside its session budget.

A bindcraft campaign piece is 6 designs at 1800 GPU-s each (~3 h) and is sent to
Modal with a 4 h ``_total_budget_hours`` -- the pipeline's own 14400 s kill, which
is what the campaign container is sized to (shared/compute_campaigns.py::
_BINDCRAFT_CAMPAIGN_CONTAINER_S). With _CAMPAIGN_SESSION_SLACK_HOURS that puts
the campaign grace at 6 h, level with the flat running cutoff.

WAS 16 designs against a 10 h budget, which bought a piece 12 h of grace. That
budget described a container the pipeline would have killed at 4 h, so the extra
6 h were spent waiting on a job that was already dead.
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


def test_a_campaign_piece_is_not_swept_before_its_own_session_budget(monkeypatch):
    plan = plan_chunks("bindcraft", 100)
    # bindcraft's chunk now fits inside the 4 h the pipeline allows it, so its
    # campaign grace (4 + 2) lands level with the flat 6 h cutoff and the
    # carve-out grants it nothing. rfantibody still declares 10 h, so it is the
    # tool that exercises the carve-out; both are asserted below.
    assert plan.chunk_size * 1800 <= 14400

    rows = [
        # Inside rfantibody's 10 h budget (+2 h slack): left alone although it
        # is past the flat 6 h cutoff. This row is the carve-out.
        {"id": "rfab-piece-7h", "status": "running", "tool": "rfantibody",
         "campaign_id": "c1", "started_at": _ago(7)},
        {"id": "rfab-piece-13h", "status": "running", "tool": "rfantibody",
         "campaign_id": "c1", "started_at": _ago(13)},
        # A bindcraft piece at 7 h IS swept: its pipeline kills the container at
        # 4 h, so 7 h means the job is already dead. Under the old 10 h budget
        # this row survived to 12 h.
        {"id": "bc-piece-7h", "status": "running", "tool": "bindcraft",
         "campaign_id": "c1", "started_at": _ago(7)},
        {"id": "bc-piece-5h", "status": "running", "tool": "bindcraft",
         "campaign_id": "c1", "started_at": _ago(5)},
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

    assert swept == ["rfab-piece-13h", "bc-piece-7h", "single-7h"]
    assert summary["running_swept"] == 3
