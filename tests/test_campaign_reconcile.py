"""Tests for ``reconcile_campaign_children`` — the poll-based terminaliser that
settles a completed atomic-pattern campaign sub-job (proteina / iggm) whose
Modal FunctionCall returned inline but posted no terminal webhook.

The critical invariant is SUCCESS-ONLY promotion: a ``succeeded`` poll (inline
``smoke_result.status == COMPLETED``, emitted only by atomic tools) is
terminalised through ``complete_job``; a ``failed`` poll is LEFT ALONE, because
a composite pilot (bindcraft / boltzgen / pxdesign / rfantibody) whose webhook
was merely delayed also reads as ``failed`` and must never be billed as a
failure from a poll. No live Modal / Supabase — the three seams are faked.
"""

from __future__ import annotations

import pytest

import shared.compute_campaigns as cc


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal PostgREST-shaped query supporting the exact chain reconcile uses:
    ``select(...).eq(...).in_(...).execute()`` filtered against the seeded rows.
    """

    def __init__(self, rows):
        self._rows = rows
        self._eq = {}
        self._in = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals):
        self._in[col] = list(vals)
        return self

    def execute(self):
        out = [
            r for r in self._rows
            if all(str(r.get(c)) == str(v) for c, v in self._eq.items())
            and all(r.get(c) in vals for c, vals in self._in.items())
        ]
        return _Resp(out)


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _Query(self._rows if name == "tool_jobs" else [])


def _fake_modal(poll_map, record):
    class _FM:
        def poll(self, fc_id):
            record.append(str(fc_id))
            return poll_map.get(
                str(fc_id),
                {"status": "running", "result": None, "gpu_seconds_used": None},
            )
    return _FM


@pytest.fixture
def recon_env(monkeypatch):
    """Wire the fake Supabase + Modal + jobs seams reconcile depends on and
    return the recorders so a test can assert what was terminalised."""
    state = {"complete": [], "running": [], "polled": []}

    def fake_complete_job(job_id, *, terminal_status, result=None,
                          error=None, gpu_seconds_used=None):
        state["complete"].append({
            "job_id": job_id,
            "terminal_status": terminal_status,
            "result": result,
            "gpu_seconds_used": gpu_seconds_used,
        })
        return None

    def fake_mark_running(job_id, *_a, **_k):
        state["running"].append(job_id)
        return True

    import shared.jobs as j
    monkeypatch.setattr(j, "complete_job", fake_complete_job)
    monkeypatch.setattr(j, "mark_running", fake_mark_running)

    def _install(rows, poll_map):
        monkeypatch.setattr(cc, "get_service_client", lambda: _Client(rows))
        import gpu.modal_client as mc
        monkeypatch.setattr(mc, "ModalClient", _fake_modal(poll_map, state["polled"]))

    return state, _install


def test_succeeded_child_is_terminalised(recon_env):
    state, install = recon_env
    rows = [{
        "id": "job-A", "status": "running", "modal_function_call_id": "fc-A",
        "campaign_id": "camp-1",
    }]
    poll_map = {"fc-A": {
        "status": "succeeded",
        "result": {"candidates": [{"rank": 1}], "candidate_count": 1},
        "gpu_seconds_used": 176,
    }}
    install(rows, poll_map)

    n = cc.reconcile_campaign_children("camp-1")

    assert n == 1
    assert len(state["complete"]) == 1
    call = state["complete"][0]
    assert call["job_id"] == "job-A"
    assert call["terminal_status"] == "succeeded"
    assert call["result"]["candidate_count"] == 1
    assert call["gpu_seconds_used"] == 176
    assert state["running"] == []


def test_failed_poll_is_left_alone(recon_env):
    """A ``failed`` poll must NOT be terminalised here — a composite pilot with
    a delayed webhook reads as failed even on success. Left to the webhook /
    stuck-job recovery, which distinguishes clean-exit from a real crash."""
    state, install = recon_env
    rows = [{
        "id": "job-B", "status": "running", "modal_function_call_id": "fc-B",
        "campaign_id": "camp-1",
    }]
    poll_map = {"fc-B": {
        "status": "failed", "result": None, "exit_code": 0,
        "gpu_seconds_used": 10,
    }}
    install(rows, poll_map)

    n = cc.reconcile_campaign_children("camp-1")

    assert n == 0
    assert state["complete"] == []
    assert state["running"] == []


def test_error_poll_is_left_alone(recon_env):
    state, install = recon_env
    rows = [{
        "id": "job-E", "status": "running", "modal_function_call_id": "fc-E",
        "campaign_id": "camp-1",
    }]
    poll_map = {"fc-E": {"status": "error", "result": None, "error": "modal down"}}
    install(rows, poll_map)

    assert cc.reconcile_campaign_children("camp-1") == 0
    assert state["complete"] == []


def test_running_pending_child_is_marked_running(recon_env):
    """A live FunctionCall on a still-``pending`` row anchors started_at."""
    state, install = recon_env
    rows = [{
        "id": "job-C", "status": "pending", "modal_function_call_id": "fc-C",
        "campaign_id": "camp-1",
    }]
    poll_map = {"fc-C": {"status": "running", "result": None}}
    install(rows, poll_map)

    assert cc.reconcile_campaign_children("camp-1") == 0
    assert state["running"] == ["job-C"]
    assert state["complete"] == []


def test_running_already_running_child_is_noop(recon_env):
    state, install = recon_env
    rows = [{
        "id": "job-R", "status": "running", "modal_function_call_id": "fc-R",
        "campaign_id": "camp-1",
    }]
    poll_map = {"fc-R": {"status": "running", "result": None}}
    install(rows, poll_map)

    assert cc.reconcile_campaign_children("camp-1") == 0
    assert state["running"] == []
    assert state["complete"] == []


def test_child_without_function_call_id_is_skipped(recon_env):
    """A row created but not yet submitted (no fc id) is never polled."""
    state, install = recon_env
    rows = [{
        "id": "job-D", "status": "pending", "modal_function_call_id": None,
        "campaign_id": "camp-1",
    }]
    install(rows, {})

    assert cc.reconcile_campaign_children("camp-1") == 0
    assert state["complete"] == []
    assert state["running"] == []


def test_mixed_batch_only_promotes_the_succeeded_one(recon_env):
    state, install = recon_env
    rows = [
        {"id": "job-A", "status": "running", "modal_function_call_id": "fc-A",
         "campaign_id": "camp-1"},
        {"id": "job-B", "status": "running", "modal_function_call_id": "fc-B",
         "campaign_id": "camp-1"},
        {"id": "job-C", "status": "pending", "modal_function_call_id": "fc-C",
         "campaign_id": "camp-1"},
        # A different campaign's child must not be touched.
        {"id": "job-Z", "status": "running", "modal_function_call_id": "fc-Z",
         "campaign_id": "camp-2"},
    ]
    poll_map = {
        "fc-A": {"status": "succeeded", "result": {"candidate_count": 3},
                 "gpu_seconds_used": 200},
        "fc-B": {"status": "failed", "result": None, "exit_code": 0},
        "fc-C": {"status": "running", "result": None},
        "fc-Z": {"status": "succeeded", "result": {"candidate_count": 9}},
    }
    install(rows, poll_map)

    n = cc.reconcile_campaign_children("camp-1")

    assert n == 1
    assert [c["job_id"] for c in state["complete"]] == ["job-A"]
    assert state["running"] == ["job-C"]
    # camp-2's child was filtered out by the campaign_id eq() before any poll:
    # reconcile must never even touch another campaign's FunctionCall.
    assert "fc-Z" not in state["polled"]
    assert set(state["polled"]) == {"fc-A", "fc-B", "fc-C"}


def test_no_service_client_returns_zero(monkeypatch):
    monkeypatch.setattr(cc, "get_service_client", lambda: None)
    assert cc.reconcile_campaign_children("camp-1") == 0


def test_no_inflight_children_returns_zero(recon_env):
    _state, install = recon_env
    install([], {})
    assert cc.reconcile_campaign_children("camp-1") == 0


def test_max_poll_caps_modal_calls(recon_env):
    """The cap bounds how many FunctionCalls a single reconcile pass touches."""
    state, install = recon_env
    rows = [
        {"id": f"job-{i}", "status": "running",
         "modal_function_call_id": f"fc-{i}", "campaign_id": "camp-1"}
        for i in range(5)
    ]
    poll_map = {
        f"fc-{i}": {"status": "succeeded", "result": {"candidate_count": 1}}
        for i in range(5)
    }
    install(rows, poll_map)

    n = cc.reconcile_campaign_children("camp-1", max_poll=2)

    assert n == 2
    assert len(state["complete"]) == 2


def test_interpret_pipeline_return_ties_the_safety_invariant():
    """The end-to-end guarantee reconcile relies on: a composite pilot's
    webhook-path return (no inline ``smoke_result``) maps to ``failed`` — never
    ``succeeded`` — so reconcile's success-only gate can NEVER be handed one.
    An atomic tool's inline ``COMPLETED`` maps to ``succeeded``. If this mapping
    ever regressed the safety story would break silently, so pin it here."""
    from gpu.modal_client import _interpret_pipeline_return

    # Composite pilot: pipeline exited clean, delivered out-of-band, no inline
    # payload. MUST NOT read as succeeded.
    webhook_shape = {
        "exit_code": 0,
        "smoke_result": None,
        "provider_job_id": "x",
        "webhook_outcome": {"delivered": False, "detail": "lost"},
    }
    assert _interpret_pipeline_return(webhook_shape)["status"] == "failed"

    # Atomic tool: inline flat COMPLETED payload. Reads as succeeded, result
    # carries the domain keys.
    atomic_shape = {
        "exit_code": 0,
        "smoke_result": {
            "status": "COMPLETED", "tier": "ligand_binder",
            "candidate_count": 8, "runtime_seconds": 176,
        },
        "provider_job_id": "y",
    }
    out = _interpret_pipeline_return(atomic_shape)
    assert out["status"] == "succeeded"
    assert out["result"]["candidate_count"] == 8
    assert out["gpu_seconds_used"] == 176


def test_complete_job_raising_is_swallowed(recon_env, monkeypatch):
    """A fault settling one child must not abort the reconcile pass or escape
    to the caller (a status read / cron tick)."""
    state, install = recon_env

    def boom(*_a, **_k):
        raise RuntimeError("settle blew up")

    import shared.jobs as j
    monkeypatch.setattr(j, "complete_job", boom)

    rows = [{
        "id": "job-A", "status": "running", "modal_function_call_id": "fc-A",
        "campaign_id": "camp-1",
    }]
    poll_map = {"fc-A": {"status": "succeeded", "result": {"candidate_count": 1}}}
    install(rows, poll_map)

    # No exception escapes; the failed settle is simply not counted.
    assert cc.reconcile_campaign_children("camp-1") == 0
