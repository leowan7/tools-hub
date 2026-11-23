"""The five-minute tick collects a finished single job nobody is watching.

Two esmfold2-design runs in November 2026 finished on Modal and were never
collected, because only the open job page polled for the result; the
six-hour sweeper then wrote them off as timeouts (the incident itself is in
docs/qa/ESMFOLD2-STALL-2026-11-05.md, not provable from here). The first
three tests are the money branches: collected and charged, collected twice,
and genuinely stuck.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from blueprints.jobs import settle_from_poll
from cron import tick_campaigns as tick_mod
from cron.tick_campaigns import collect_finished_jobs
from shared import jobs as jobs_mod
from shared.jobs import ToolJob, timeout_stuck_job
from tests.test_sweep_stuck_recovery import (
    _fake_client_factory,
    _FakeJobsStore,
    _row,
)

pytestmark = pytest.mark.usefixtures("isolate_supabase")

FC = "fc-01REALCALL"
CANDIDATES = [
    {"rank": i, "name": f"design_{i}", "pdb_key": f"designs/design_{i}_complex.pdb",
     "sequence": "ACDE", "scores": {"ipTM": 0.9 - i / 10}}
    for i in range(3)
]
SUCCEEDED = {
    "status": "succeeded",
    "result": {"candidates": CANDIDATES, "designs_completed": 3,
               "runtime_seconds": 1107},
    "gpu_seconds_used": 1107,
    "exit_code": 0,
    "error": None,
}


class _ListClient:
    """The one chain collect_finished_jobs runs, filtered against the store."""

    def __init__(self, store):
        self._store = store
        self._statuses = None
        self._null = []

    def table(self, _name):
        return self

    def select(self, *_a):
        return self

    def in_(self, _col, values):
        self._statuses = list(values)
        return self

    def is_(self, col, _val):
        self._null.append(col)
        return self

    def order(self, *_a):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return SimpleNamespace(data=[
            dict(r) for r in self._store.rows.values()
            if r["status"] in self._statuses
            and all(r.get(c) is None for c in self._null)
        ])


@pytest.fixture
def env():
    """A running, live-charged esmfold2-design row behind the real
    complete_job, with the wallet settle and the email recorded."""
    row = _row(
        tool="esmfold2-design", preset="minibinder", modal_function_call_id=FC,
        inputs={"_wallet": {"live": True, "hold_tx_id": 1393},
                "_progress": {"stage": "folding"}},
    )
    store = _FakeJobsStore([row])
    emails: list = []
    polls = {FC: SUCCEEDED}
    modal = SimpleNamespace(poll=lambda fc: polls[fc])
    with patch.object(jobs_mod, "get_service_client", _fake_client_factory(store)), \
         patch("shared.credits.get_service_client", lambda: _ListClient(store)), \
         patch("shared.wallet.settle_live_run") as settle, \
         patch.object(jobs_mod, "_send_completion_email", emails.append):
        yield SimpleNamespace(
            store=store, id=row["id"], row=row, emails=emails, settle=settle,
            polls=polls, modal=modal,
        )


def test_a_finished_run_is_collected_and_charged(env):
    assert collect_finished_jobs(modal_client=env.modal) == 1

    stored = env.store.rows[env.id]
    assert stored["status"] == "succeeded"
    assert stored["failure_class"] == "succeeded"
    assert len(stored["result"]["candidates"]) == 3
    assert stored["gpu_seconds_used"] == 1107
    env.settle.assert_called_once()
    assert 1107 in (*env.settle.call_args.args, *env.settle.call_args.kwargs.values())
    assert "failure_reason" not in env.settle.call_args.kwargs or (
        env.settle.call_args.kwargs["failure_reason"] is None
    )
    assert len(env.emails) == 1


def test_collecting_twice_settles_and_emails_once(env):
    """A second tick no longer selects the row, and an open job page holding
    a snapshot from before the settle hits complete_job's already-terminal
    return."""
    stale = ToolJob.from_row(env.row)

    assert collect_finished_jobs(modal_client=env.modal) == 1
    assert collect_finished_jobs(modal_client=env.modal) == 0
    settle_from_poll(stale, SUCCEEDED)

    env.settle.assert_called_once()
    assert len(env.emails) == 1


@pytest.mark.parametrize("poll", [
    {"status": "running", "result": None, "gpu_seconds_used": None, "error": None},
    {"status": "error", "result": None, "gpu_seconds_used": None, "error": "boom"},
    # A webhook-path pipeline's poll: failed, no inline payload.
    {"status": "failed", "result": None, "gpu_seconds_used": None,
     "exit_code": 0, "error_bucket": None, "error": "webhook not delivered"},
])
def test_a_run_modal_has_not_finished_is_left_for_the_sweeper(env, poll):
    env.polls[FC] = poll

    assert collect_finished_jobs(modal_client=env.modal) == 0

    assert env.store.rows[env.id]["status"] == "running"
    env.settle.assert_not_called()
    assert env.emails == []


def test_the_sweeper_still_refunds_a_run_that_hung(env):
    """After the tick has passed a hung run over, the six-hour path ends it
    as a refunded timeout exactly as before."""
    env.polls[FC] = {"status": "running", "result": None,
                     "gpu_seconds_used": None, "error": None}
    collect_finished_jobs(modal_client=env.modal)

    with patch("gpu.modal_client.ModalClient", lambda: env.modal), \
         patch("shared.job_recovery._list_prefix_names", return_value=[]):
        assert timeout_stuck_job(env.id) == "timed_out"

    stored = env.store.rows[env.id]
    assert stored["status"] == "timeout"
    assert stored["failure_class"] == "no_progress_timeout"
    env.settle.assert_called_once()
    assert env.emails == []


def test_a_queued_run_stays_pending_for_the_thirty_minute_sweep(env):
    env.store.rows[env.id]["status"] = "pending"
    env.polls[FC] = {"status": "running", "result": None,
                     "gpu_seconds_used": None, "error": None}

    assert collect_finished_jobs(modal_client=env.modal) == 0

    assert env.store.rows[env.id]["status"] == "pending"


def test_an_inline_failure_is_settled_as_failed(env):
    env.polls[FC] = {"status": "failed", "result": None, "gpu_seconds_used": 40,
                     "exit_code": 1, "error_bucket": "preflight",
                     "error": "preflight:weights - mismatch"}

    assert collect_finished_jobs(modal_client=env.modal) == 1

    assert env.store.rows[env.id]["status"] == "failed"
    env.settle.assert_called_once()


def test_campaign_children_and_unsubmitted_rows_are_not_polled(env):
    child = _row(campaign_id="camp-1", modal_function_call_id="fc-child")
    unsubmitted = _row(status="pending", modal_function_call_id=None)
    env.store.rows[child["id"]] = child
    env.store.rows[unsubmitted["id"]] = unsubmitted
    polled: list = []
    modal = SimpleNamespace(poll=lambda fc: polled.append(fc) or SUCCEEDED)

    collect_finished_jobs(modal_client=modal)

    assert polled == [FC]


def test_the_tick_runs_the_collect_pass(monkeypatch):
    client = MagicMock()
    client.table.side_effect = RuntimeError("stop after the collect pass")
    monkeypatch.setattr("shared.credits.get_service_client", lambda: client)
    monkeypatch.setattr(jobs_mod, "meter_live_runs",
                        lambda: {"errors": []})
    monkeypatch.setattr(tick_mod, "collect_finished_jobs", lambda: 2)

    assert tick_mod.tick_campaigns()["collected"] == 2
