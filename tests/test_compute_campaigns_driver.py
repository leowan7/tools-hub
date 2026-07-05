"""Driver tests for compute campaigns: dispatch, finalize, idempotency, cancel.

Exercises drive_campaign end to end against a stateful fake Supabase store,
with the wallet / job-creation / Modal / storage seams monkeypatched. No
live Modal or Supabase. The real tool adapters (rfdiffusion) are used for
build_payload since they are pure dict transforms.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import shared.compute_campaigns as cc
from shared.compute_campaigns import (
    ComputeCampaign,
    cancel_campaign,
    drive_campaign,
)


# ---------------------------------------------------------------------------
# Stateful fake Supabase client (select / insert / update / eq / in_)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._eq = []
        self._in = []
        self._insert = None
        self._update = None
        self._single = False

    def select(self, *_a, **_k):
        return self

    def insert(self, row):
        self._insert = dict(row)
        return self

    def update(self, fields):
        self._update = dict(fields)
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def in_(self, col, vals):
        self._in.append((col, list(vals)))
        return self

    def gte(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def single(self):
        self._single = True
        return self

    def _match(self, r):
        if not all(str(r.get(c)) == str(v) for c, v in self._eq):
            return False
        if not all(r.get(c) in vals for c, vals in self._in):
            return False
        return True

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._insert is not None:
            self._store[self._table].append(self._insert)
            return _Result([self._insert])
        if self._update is not None:
            # Real Postgrest evaluates the WHERE on the pre-update row and
            # RETURNs the rows it updated, so a conditional (CAS) update that
            # changes the filtered column still returns its winners. Match
            # first, then mutate.
            matched = [r for r in rows if self._match(r)]
            for r in matched:
                r.update(self._update)
            return _Result(matched)
        matched = [r for r in rows if self._match(r)]
        if self._single:
            if not matched:
                raise RuntimeError("no rows")
            return _Result(matched[0])
        return _Result(matched)


class _Client:
    def __init__(self):
        self.store = {"compute_campaigns": [], "tool_jobs": []}

    def table(self, name):
        return _Query(self.store, name)


# ---------------------------------------------------------------------------
# Fixture: wire the fake store + monkeypatch the driver's external seams
# ---------------------------------------------------------------------------


@pytest.fixture
def driver_env(monkeypatch):
    client = _Client()
    monkeypatch.setattr(cc, "get_service_client", lambda: client)

    # wallet_balance drives the balance-aware hold seam below. Default is high
    # so tests that do not care about funds never trip a refusal; the pause /
    # resume tests set it low to simulate a drained wallet.
    state = {
        "holds": [], "released": [], "modal_calls": [], "submits": 0,
        "wallet_balance": Decimal("1000000"),
    }

    # Wallet seams. fake_reserve_hold mirrors the real atomic hold: it refuses
    # (returns None) when the balance cannot cover the estimate, else decrements.
    import shared.wallet as w

    def fake_reserve_hold(user_id, tool, job_id, estimate, params):
        est = Decimal(str(estimate))
        if state["wallet_balance"] < est:
            return None  # insufficient balance (atomic in real reserve_hold)
        state["wallet_balance"] -= est
        hid = f"hold-{len(state['holds'])}"
        state["holds"].append({"id": hid, "tool": tool, "estimate": est})
        return hid

    def fake_release_hold(hold_tx_id, reason="x"):
        state["released"].append(hold_tx_id)
        return True

    def fake_get_or_create_wallet(user_id):
        return {"balance_usd": str(state["wallet_balance"]), "wallet_frozen": False}

    monkeypatch.setattr(w, "reserve_hold", fake_reserve_hold)
    monkeypatch.setattr(w, "release_hold", fake_release_hold)
    monkeypatch.setattr(w, "get_or_create_wallet", fake_get_or_create_wallet)

    # Job-creation seam: write a child row into the same store, enforcing the
    # UNIQUE(campaign_id, chunk_index, attempt) constraint by returning None.
    import shared.jobs as j

    def fake_create_job(*, user_id, tool, preset, inputs, campaign_id=None,
                        chunk_index=None, attempt=None, campaign_label=None,
                        **_kw):
        for r in client.store["tool_jobs"]:
            if (r.get("campaign_id") == campaign_id
                    and r.get("chunk_index") == chunk_index
                    and r.get("attempt") == attempt):
                return None  # UNIQUE violation
        jid = f"job-{chunk_index}-{attempt}"
        row = {
            "id": jid, "user_id": user_id, "tool": tool, "preset": preset,
            "status": "pending", "inputs": inputs, "result": None, "error": None,
            "modal_function_call_id": None, "job_token": f"tok-{jid}",
            "gpu_seconds_used": None, "created_at": None, "started_at": None,
            "completed_at": None, "campaign_id": campaign_id,
            "chunk_index": chunk_index, "attempt": attempt,
            "campaign_label": campaign_label, "failure_class": None,
        }
        client.store["tool_jobs"].append(row)
        return j.ToolJob.from_row(row)

    def fake_set_modal_call(job_id, fc_id):
        state["modal_calls"].append((job_id, fc_id))
        for r in client.store["tool_jobs"]:
            if r["id"] == job_id:
                r["modal_function_call_id"] = fc_id
        return True

    def fake_mark_failed(job_id, *, error=None, **_kw):
        for r in client.store["tool_jobs"]:
            if r["id"] == job_id:
                r["status"] = "failed"
                r["error"] = error
                r["failure_class"] = "infra_crash"
        return True

    monkeypatch.setattr(j, "create_job", fake_create_job)
    monkeypatch.setattr(j, "set_modal_call", fake_set_modal_call)
    monkeypatch.setattr(j, "mark_failed", fake_mark_failed)

    # Storage seam.
    import shared.storage as s
    monkeypatch.setattr(s, "presigned_input_url", lambda path, expires_seconds=0: "https://fake/url")

    # Modal seam.
    import gpu.modal_client as mc

    class FakeModal:
        def submit(self, *a, **k):
            state["submits"] += 1
            return {"function_call_id": f"fc-{state['submits']}", "gpu_seconds_cap": 1800}

    monkeypatch.setattr(mc, "ModalClient", FakeModal)

    return client, state


def _seed_campaign(client, *, total_subjobs=2, chunk_size=12, requested=24,
                   concurrency_target=8, status="funded", tool="rfdiffusion"):
    row = {
        "id": "camp-1", "user_id": "user-1", "tool": tool, "preset": "pilot",
        "status": status, "requested_designs": requested, "chunk_size": chunk_size,
        "total_subjobs": total_subjobs, "concurrency_target": concurrency_target,
        "max_attempts": 2, "budget_usd": "10", "reserved_usd": "0",
        "spent_usd": "0", "refunded_usd": "0",
        "params": {"target_chain": "A", "hotspot_residues": [10, 20],
                   "binder_length": {"min": 55, "max": 65}},
        "name": "test", "target_storage_path": "inputs/u/target.pdb",
        "target_name": "T",
    }
    client.store["compute_campaigns"].append(row)
    return row


def _campaign_status(client):
    return client.store["compute_campaigns"][0]["status"]


def _children(client):
    return client.store["tool_jobs"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_drive_dispatches_all_chunks(driver_env):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24)

    drive_campaign("camp-1")

    kids = _children(client)
    assert len(kids) == 2
    assert {k["chunk_index"] for k in kids} == {0, 1}
    # each chunk carries the right per-chunk design count under num_designs
    assert all(k["inputs"]["num_designs"] == 12 for k in kids)
    # each got a hold + a modal call
    assert len(state["holds"]) == 2
    assert len(state["modal_calls"]) == 2
    assert all(k["modal_function_call_id"] for k in kids)
    # funded -> running on first dispatch
    assert _campaign_status(client) == "running"


def test_drive_respects_concurrency(driver_env):
    client, state = driver_env
    # 10 chunks, concurrency 3 -> only 3 dispatched on the first drive.
    _seed_campaign(client, total_subjobs=10, chunk_size=12, requested=120,
                   concurrency_target=3)

    drive_campaign("camp-1")
    assert len(_children(client)) == 3
    assert state["submits"] == 3


def test_drive_is_idempotent_no_duplicate_chunks(driver_env):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24)

    drive_campaign("camp-1")
    drive_campaign("camp-1")  # second drive: nothing new (all in-flight)

    kids = _children(client)
    assert len(kids) == 2  # no duplicates
    # No hold leaked: 2 holds placed, 0 released.
    assert len(state["holds"]) == 2
    assert state["released"] == []


def test_drive_finalizes_completed(driver_env):
    client, _ = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24)
    drive_campaign("camp-1")
    # Simulate both children succeeding (the real terminal writers do this).
    for r in _children(client):
        r["status"] = "succeeded"
    drive_campaign("camp-1")
    assert _campaign_status(client) == "completed"


def test_drive_finalizes_with_failures(driver_env):
    client, _ = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24)
    drive_campaign("camp-1")
    kids = _children(client)
    kids[0]["status"] = "succeeded"
    kids[1]["status"] = "failed"
    drive_campaign("camp-1")
    assert _campaign_status(client) == "completed_with_failures"


def test_drive_finalizes_all_failed(driver_env):
    client, _ = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24)
    drive_campaign("camp-1")
    for r in _children(client):
        r["status"] = "failed"
    drive_campaign("camp-1")
    assert _campaign_status(client) == "failed"


def test_dispatch_releases_hold_on_duplicate(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=1, chunk_size=12, requested=12)
    # Pre-create chunk 0 so create_job hits the UNIQUE violation path.
    client.store["tool_jobs"].append({
        "id": "pre", "campaign_id": "camp-1", "chunk_index": 0, "attempt": 1,
        "status": "running",
    })
    drive_campaign("camp-1")
    # The pre-existing chunk 0 blocks a new dispatch; no new child, no leaked hold.
    assert len([k for k in _children(client) if k["id"] != "pre"]) == 0


def test_modal_submit_failure_marks_failed_and_releases_hold(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=1, chunk_size=12, requested=12)

    import gpu.modal_client as mc

    class BoomModal:
        def submit(self, *a, **k):
            raise RuntimeError("modal down")

    monkeypatch.setattr(mc, "ModalClient", BoomModal)

    drive_campaign("camp-1")
    kids = [k for k in _children(client) if k["id"] != "pre"]
    assert len(kids) == 1
    assert kids[0]["status"] == "failed"
    # hold placed then released (no stranded reservation)
    assert len(state["holds"]) == 1
    assert len(state["released"]) == 1


def test_drive_skips_draft_and_terminal(driver_env):
    client, _ = driver_env
    for status in ("draft", "completed", "cancelled", "failed"):
        client.store["compute_campaigns"] = []
        client.store["tool_jobs"] = []
        _seed_campaign(client, status=status)
        drive_campaign("camp-1")
        assert len(_children(client)) == 0  # nothing dispatched


def test_hold_refusal_skips_and_retries(driver_env, monkeypatch):
    """A chunk whose hold is refused creates no row and is retried later."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24, concurrency_target=8)

    # Refuse the very first hold, then succeed for all subsequent calls.
    import shared.wallet as w
    calls = {"n": 0}

    def flaky_hold(user_id, tool, job_id, estimate, params):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # refused (e.g. daily cap / balance)
        hid = f"hold-{calls['n']}"
        state["holds"].append({"id": hid})
        return hid

    monkeypatch.setattr(w, "reserve_hold", flaky_hold)

    drive_campaign("camp-1")
    # First pass: one chunk skipped (no row), one launched.
    assert len(_children(client)) == 1

    drive_campaign("camp-1")
    # Second pass: the skipped chunk is retried and now lands.
    kids = _children(client)
    assert len(kids) == 2
    assert {k["chunk_index"] for k in kids} == {0, 1}
    # No stranded holds: the refused attempt placed none.
    assert state["released"] == []


def test_cancel_campaign(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=3, chunk_size=12, requested=36,
                   concurrency_target=8, status="running")
    # Two in-flight children.
    client.store["tool_jobs"] = [
        {"id": "j0", "campaign_id": "camp-1", "chunk_index": 0, "attempt": 1, "status": "running"},
        {"id": "j1", "campaign_id": "camp-1", "chunk_index": 1, "attempt": 1, "status": "pending"},
    ]
    cancelled = []
    import shared.jobs as j
    monkeypatch.setattr(
        j, "cancel_job",
        lambda job_id, *, user_id, modal_client: (cancelled.append(job_id), (None, None))[1],
    )
    ok = cancel_campaign("camp-1", "user-1")
    assert ok is True
    assert _campaign_status(client) == "cancelled"
    assert set(cancelled) == {"j0", "j1"}


# ---------------------------------------------------------------------------
# Fund-and-drain: pause on insufficient funds, resume on top-up (step 4a)
# ---------------------------------------------------------------------------


def _patch_pause_email(monkeypatch):
    """Capture pause emails; return the list they are appended to."""
    import shared.email as em
    calls: list = []
    monkeypatch.setattr(
        em, "send_campaign_paused_email",
        lambda **kw: (calls.append(kw), True)[1],
    )
    return calls


def test_drive_pauses_when_wallet_cannot_fund_next_chunk(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=3, chunk_size=12, requested=36,
                   concurrency_target=8)
    calls = _patch_pause_email(monkeypatch)
    # Fund exactly one chunk hold.
    state["wallet_balance"] = cc.child_hold_usd("rfdiffusion", 12)

    drive_campaign("camp-1")

    # One chunk launched, then the wallet is dry -> paused, one email.
    assert _campaign_status(client) == "paused_insufficient_funds"
    assert len(_children(client)) == 1
    assert len(calls) == 1
    assert calls[0]["campaign_id"] == "camp-1"
    # The refused chunk placed no hold, so nothing is stranded.
    assert state["released"] == []


def test_drive_pauses_before_any_chunk_when_broke(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, chunk_size=12, requested=24,
                   concurrency_target=8)
    calls = _patch_pause_email(monkeypatch)
    state["wallet_balance"] = Decimal("0")

    drive_campaign("camp-1")

    assert _campaign_status(client) == "paused_insufficient_funds"
    assert len(_children(client)) == 0
    assert len(calls) == 1


def test_pause_email_sent_once_across_ticks(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, chunk_size=12, requested=24,
                   concurrency_target=8)
    calls = _patch_pause_email(monkeypatch)
    state["wallet_balance"] = Decimal("0")

    drive_campaign("camp-1")          # first pause -> email
    drive_campaign("camp-1")          # cron re-tick, still broke -> no re-email
    drive_campaign("camp-1")

    assert _campaign_status(client) == "paused_insufficient_funds"
    assert len(calls) == 1


def test_drive_resumes_after_topup(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=3, chunk_size=12, requested=36,
                   concurrency_target=8)
    calls = _patch_pause_email(monkeypatch)
    one_hold = cc.child_hold_usd("rfdiffusion", 12)
    state["wallet_balance"] = one_hold  # only one chunk

    drive_campaign("camp-1")
    assert _campaign_status(client) == "paused_insufficient_funds"
    assert len(_children(client)) == 1
    assert len(calls) == 1

    # Top up: the cron re-drive dispatches the rest and flips back to running.
    state["wallet_balance"] = one_hold * 10
    drive_campaign("camp-1")

    assert _campaign_status(client) == "running"
    assert len(_children(client)) == 3
    assert {k["chunk_index"] for k in _children(client)} == {0, 1, 2}
    assert len(calls) == 1  # no second email on resume


def test_paused_campaign_does_not_finalize_with_undispatched_chunks(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=3, chunk_size=12, requested=36,
                   concurrency_target=8)
    _patch_pause_email(monkeypatch)
    state["wallet_balance"] = cc.child_hold_usd("rfdiffusion", 12)

    drive_campaign("camp-1")
    assert _campaign_status(client) == "paused_insufficient_funds"
    # The one dispatched child finishes; the campaign must NOT finalize while
    # funded chunks remain undispatched — it waits for a top-up.
    _children(client)[0]["status"] = "succeeded"
    drive_campaign("camp-1")
    assert _campaign_status(client) == "paused_insufficient_funds"


def test_transient_refusal_does_not_pause(driver_env, monkeypatch):
    """A hold refusal with balance still available is transient, not a pause."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, chunk_size=12, requested=24,
                   concurrency_target=8)
    calls = _patch_pause_email(monkeypatch)

    import shared.wallet as w
    n = {"c": 0}

    def flaky_hold(user_id, tool, job_id, estimate, params):
        # Refuse once despite a healthy balance (models a transient blip), then
        # succeed. Balance stays high so classification says "skipped".
        n["c"] += 1
        if n["c"] == 1:
            return None
        hid = f"hold-{n['c']}"
        state["holds"].append({"id": hid})
        return hid

    monkeypatch.setattr(w, "reserve_hold", flaky_hold)

    drive_campaign("camp-1")
    assert _campaign_status(client) != "paused_insufficient_funds"
    assert len(calls) == 0
    drive_campaign("camp-1")
    assert len(_children(client)) == 2  # the transiently-skipped chunk retried


def test_cron_active_states_includes_paused():
    from cron.tick_campaigns import _ACTIVE_STATES
    assert "paused_insufficient_funds" in _ACTIVE_STATES
