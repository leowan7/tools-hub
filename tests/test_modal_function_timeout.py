"""A Modal CONTAINER timeout must terminalise the job, not strand it.

``modal.exception.FunctionTimeoutError`` is what Modal raises out of
``FunctionCall.get`` when the function hit its own container timeout
(``modal/_utils/function_utils.py::_process_result`` on
``GENERIC_STATUS_TIMEOUT``). It is NOT a subclass of ``builtins.TimeoutError``
-- its base is ``modal.exception.TimeoutError``, a separate hierarchy -- so
``ModalClient.poll``'s ``except TimeoutError`` ("still running") never saw it
and it fell through to the generic ``except Exception`` as ``status="error"``.

Before this fix ``blueprints/jobs.py::job_status`` branched only on succeeded
/ failed / running, so an "error" poll left the row NON-TERMINAL: the wallet
hold was neither settled nor released and the user watched a spinner until
``cron/sweep_stuck_jobs.py`` caught it at ``STUCK_RUNNING_AGE_HOURS``
(default 6). That sweeper is the only other terminaliser ``esmfold2-design``
has -- its ``WEBHOOK_URL`` is documented "unused at launch" and heartbeats are
still a TODO (``tools/esmfold2_design/run_pipeline.py:13``, ``:93``).

The exception hierarchy is asserted against the INSTALLED modal, not restated
from the docs, so this file reports it if a future modal release reparents the
class (which would make the old code accidentally correct and this fix inert).

Not covered here because ``tests/test_modal_call_deadline.py`` already owns it:
our own ``ModalCallTimeout`` (the 90 s wall-clock cap on a wedged channel) is a
builtins.TimeoutError subclass, is re-raised ahead of the "still running" arm,
and reports "error" -- see ``test_a_wedged_get_reports_error_not_running`` and
``test_modals_own_not_finished_yet_is_still_running`` there.
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

import modal
import modal.exception

from gpu.modal_client import ModalClient

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _fake_function_call(exc):
    """Stand in for ``modal.FunctionCall`` whose ``.get()`` raises ``exc``."""

    class _FC:
        @staticmethod
        def from_id(_fc_id):
            return _FC()

        def get(self, timeout=None, **_kw):
            raise exc

    return _FC


# ---------------------------------------------------------------------------
# 1. The premise: the two TimeoutErrors are unrelated classes.
# ---------------------------------------------------------------------------


def test_modal_function_timeout_is_not_a_builtin_timeout_error():
    assert not issubclass(
        modal.exception.FunctionTimeoutError, builtins.TimeoutError
    ), (
        "modal reparented FunctionTimeoutError under builtins.TimeoutError; "
        "poll()'s explicit clause is now redundant but must STAY AHEAD of the "
        "`except TimeoutError` arm, or a container timeout polls as 'running' "
        "forever."
    )
    assert issubclass(
        modal.exception.FunctionTimeoutError, modal.exception.TimeoutError
    )


# ---------------------------------------------------------------------------
# 2. poll() maps the container timeout to the terminal "timeout" status.
# ---------------------------------------------------------------------------


def test_poll_maps_function_timeout_to_timeout_status(monkeypatch):
    monkeypatch.setattr(
        modal,
        "FunctionCall",
        _fake_function_call(
            modal.exception.FunctionTimeoutError("Function exceeded timeout")
        ),
    )
    out = ModalClient().poll("fc-abc123")
    assert out["status"] == "timeout", (
        "a container timeout polled as %r; no caller terminalises that, so the "
        "job row stays non-terminal with its wallet hold unsettled"
        % out["status"]
    )
    assert out["result"] is None
    assert "timeout" in (out["error"] or "").lower()


# ---------------------------------------------------------------------------
# 3. The status route terminalises a "timeout" poll, through the recovery gate.
# ---------------------------------------------------------------------------


@pytest.fixture
def status_route(monkeypatch):
    """Flask test client for ``/jobs/<id>/status.json`` with get_job, the two
    terminalisers and the user context faked. Returns (client, calls)."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    calls: list[tuple] = []
    row = SimpleNamespace(
        id="job-1",
        status="running",
        tool="esmfold2-design",
        preset="standard",
        inputs={},
        gpu_seconds_used=None,
        started_at=None,
        modal_function_call_id="fc-abc123",
    )

    def fake_get_job(job_id, user_id=None, **_kw):
        return row

    def fake_timeout_stuck_job(job_id, *, probe_modal=True):
        calls.append(("timeout_stuck_job", job_id, probe_modal))
        row.status = "timeout"
        return "timed_out"

    def fake_complete_job(job_id, *, terminal_status, **_kw):
        calls.append(("complete_job", job_id, terminal_status))
        row.status = terminal_status
        return row

    monkeypatch.setattr("blueprints.jobs.get_job", fake_get_job)
    monkeypatch.setattr("blueprints.jobs.complete_job", fake_complete_job)
    # raising=False so the fixture still builds against a blueprints/jobs.py
    # that has not imported timeout_stuck_job yet: the timeout test then fails
    # on its assertion (nothing was called) instead of erroring in setup.
    monkeypatch.setattr(
        "blueprints.jobs.timeout_stuck_job", fake_timeout_stuck_job, raising=False
    )
    monkeypatch.setattr(
        "blueprints.jobs.load_user_context",
        lambda: SimpleNamespace(
            user_id="u-1", tier="free", balance=10, email="user@example.com"
        ),
    )

    flask_app = create_app()
    flask_app.config["TESTING"] = True

    def _set_poll(status):
        flask_app.modal_client = SimpleNamespace(
            poll=lambda _fc: {
                "status": status,
                "result": None,
                "gpu_seconds_used": None,
                "error": "Modal function timeout: Function exceeded timeout",
            }
        )

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_email"] = "user@example.com"
    return client, calls, _set_poll


def test_status_route_terminalises_a_timeout_poll(status_route):
    client, calls, set_poll = status_route
    set_poll("timeout")
    resp = client.get("/jobs/job-1/status.json")
    assert resp.status_code == 200
    # timeout_stuck_job, not complete_job(terminal_status="timeout"): it runs
    # recover_stuck_job_result first, so a run that finished its designs and
    # uploaded them before the container clock expired is finalised as
    # succeeded off Storage rather than refunded with its results discarded.
    # probe_modal=False: recovery must not spend a SECOND bounded 90 s Modal
    # call in this request -- two stacked hops is 180 s, past gunicorn's 120 s
    # worker watchdog (gunicorn.conf.py::timeout). The re-probe could only
    # re-raise the same FunctionTimeoutError and reach the same "unknown"
    # verdict.
    assert calls == [("timeout_stuck_job", "job-1", False)], (
        "a 'timeout' poll left the job row untouched; the wallet hold is "
        "neither settled nor released until the 6h stuck-job sweeper"
    )
    assert resp.get_json()["status"] == "timeout"


def test_status_route_leaves_an_error_poll_alone(status_route):
    """An "error" poll is the bucket for an unreachable or wedged Modal API
    call, which says nothing about whether the GPU run stopped. Terminalising
    it would refund and close a job still burning GPU."""
    client, calls, set_poll = status_route
    set_poll("error")
    resp = client.get("/jobs/job-1/status.json")
    assert resp.status_code == 200
    assert calls == []
    assert resp.get_json()["status"] == "running"


# ---------------------------------------------------------------------------
# 4. probe_modal=False actually skips the second Modal round trip.
# ---------------------------------------------------------------------------


def test_recovery_skips_the_modal_reprobe_when_told_to(monkeypatch):
    """The route's probe_modal=False must SKIP _probe_modal, not just be
    accepted as a keyword: the second _bounded_modal_call is the 90 s that
    stacks past gunicorn's 120 s worker watchdog."""
    import shared.job_recovery as jr

    probes: list = []

    def boom(job):
        probes.append(job)
        raise AssertionError("re-probed Modal in a request that already polled")

    monkeypatch.setattr(jr, "_probe_modal", boom)
    # No _progress snapshot -> _completion_signal "unknown" -> nothing to
    # recover, so the call returns None without ever touching Storage.
    job = SimpleNamespace(
        id="job-1", inputs={}, modal_function_call_id="fc-abc123"
    )
    assert jr.recover_stuck_job_result(job, probe_modal=False) is None
    assert probes == []

    # Control: the sweeper's default path DOES probe.
    def record(job):
        probes.append(job)
        return None, "unknown"

    monkeypatch.setattr(jr, "_probe_modal", record)
    assert jr.recover_stuck_job_result(job) is None
    assert len(probes) == 1
