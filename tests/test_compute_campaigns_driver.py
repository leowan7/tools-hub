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
    cancel_campaign,
    drive_campaign,
)


# ---------------------------------------------------------------------------
# Stateful fake Supabase client (select / insert / update / eq / in_)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._eq = []
        self._in = []
        self._insert = None
        self._update = None
        self._single = False
        self._count = None
        self._head = False
        self._lt = []
        self._is = []

    def select(self, *_a, **_k):
        self._count = _k.get("count")
        self._head = bool(_k.get("head", False))
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

    def lt(self, col, val):
        self._lt.append((col, val))
        return self

    def is_(self, col, val):
        self._is.append((col, val))  # val == "null" -> IS NULL
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
        for c, v in self._lt:
            rv = r.get(c)
            if rv is None or not (str(rv) < str(v)):  # ISO timestamps sort lexically
                return False
        for c, v in self._is:
            if v == "null" and r.get(c) is not None:
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
        if self._count is not None:
            # count="exact" returns the row count; head=True omits the rows.
            return _Result([] if self._head else matched, count=len(matched))
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
                   concurrency_target=8, status="funded", tool="rfdiffusion",
                   campaign_id="camp-1", user_id="user-1"):
    row = {
        "id": campaign_id, "user_id": user_id, "tool": tool, "preset": "pilot",
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


def test_dispatch_resyncs_frontier_on_concurrent_duplicate(driver_env, monkeypatch):
    """A concurrent driver claiming the frontier index: create_job returns None,
    the hold is released, and the driver resyncs the frontier (count advanced)
    and moves to the next hole instead of stalling."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, chunk_size=12, requested=24)

    import shared.jobs as j
    real_create = j.create_job  # the driver_env fake

    def racing_create(*, chunk_index=None, **kw):
        # Simulate a concurrent driver winning chunk 0: insert its row and
        # return None (the UNIQUE-violation signal) for our first attempt on 0.
        if chunk_index == 0 and not any(
            r.get("chunk_index") == 0 for r in client.store["tool_jobs"]
        ):
            client.store["tool_jobs"].append({
                "id": "concurrent-0", "campaign_id": "camp-1", "chunk_index": 0,
                "attempt": 1, "status": "running",
            })
            return None
        return real_create(chunk_index=chunk_index, **kw)

    monkeypatch.setattr(j, "create_job", racing_create)
    drive_campaign("camp-1")

    kids = _children(client)
    # Frontier resynced past the duplicate: chunk 0 is the concurrent row, chunk
    # 1 is ours. No hole, no stall.
    assert {k["chunk_index"] for k in kids} == {0, 1}
    assert any(k["id"] == "concurrent-0" for k in kids)
    # Exactly the duplicate's hold was released; our chunk 1 keeps its hold.
    assert len(state["released"]) == 1


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


def test_transient_hold_refusal_retries_same_index_next_pass(driver_env, monkeypatch):
    """A transient hold refusal breaks the pass at the frontier (no skip-past,
    so no hole forms) and the SAME chunk is retried on the next drive. Nothing
    is lost."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24, concurrency_target=8)

    # Refuse the very first hold, then succeed for all subsequent calls. The
    # wallet still reports a healthy balance (driver_env default), so the refusal
    # classifies as transient ("skipped"), not insufficient funds.
    import shared.wallet as w
    calls = {"n": 0}

    def flaky_hold(user_id, tool, job_id, estimate, params):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # transient refusal on the first attempt
        hid = f"hold-{calls['n']}"
        state["holds"].append({"id": hid})
        return hid

    monkeypatch.setattr(w, "reserve_hold", flaky_hold)

    drive_campaign("camp-1")
    # First pass: the frontier chunk's hold is refused -> break, no rows created.
    assert len(_children(client)) == 0

    drive_campaign("camp-1")
    # Second pass: chunk 0 is retried and both chunks land, in order, no gap.
    kids = _children(client)
    assert len(kids) == 2
    assert {k["chunk_index"] for k in kids} == {0, 1}
    # No stranded holds: the refused attempt placed none.
    assert state["released"] == []


def test_presign_failure_skips_dispatch_without_spending(driver_env, monkeypatch):
    """A presign failure must cost nothing and dispatch nothing.

    Regression for the swallowed-presign bug: the except used to leave
    presigned_url = "" and fall through to build_payload + submit, so the
    driver kept placing per-child holds and launching GPU containers with no
    input file until the whole campaign had dispatched and failed. Storage is
    re-presigned every wave, so one outage burned the entire budget silently.
    """
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24, concurrency_target=8)

    import shared.storage as s
    calls = {"n": 0}

    def flaky_presign(path, expires_seconds=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("storage down")
        return "https://fake/url"

    monkeypatch.setattr(s, "presigned_input_url", flaky_presign)

    drive_campaign("camp-1")
    # Nothing created, nothing charged, nothing launched.
    assert len(_children(client)) == 0
    assert state["holds"] == []
    assert state["submits"] == 0
    # And no hold was placed then released — the money never moved at all.
    assert state["released"] == []

    drive_campaign("camp-1")
    # Storage recovered: the same chunks are retried, in order, no gap.
    kids = _children(client)
    assert len(kids) == 2
    assert {k["chunk_index"] for k in kids} == {0, 1}


def test_presign_returning_empty_skips_dispatch(driver_env, monkeypatch):
    """A falsy presign return is the same unrunnable state as a raise, so it
    must take the same no-spend path rather than submitting an empty URL."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24, concurrency_target=8)

    import shared.storage as s
    monkeypatch.setattr(s, "presigned_input_url", lambda path, expires_seconds=0: "")

    drive_campaign("camp-1")

    assert len(_children(client)) == 0
    assert state["holds"] == []
    assert state["submits"] == 0


def test_campaign_without_target_still_dispatches(driver_env):
    """The presign guard must not break tools that legitimately have no staged
    target (a proteina curated-task campaign carries target_storage_path=None)."""
    client, state = driver_env
    row = _seed_campaign(client, total_subjobs=2, requested=24, concurrency_target=8)
    row["target_storage_path"] = None

    drive_campaign("camp-1")

    assert len(_children(client)) == 2
    assert state["submits"] == 2


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


# ---------------------------------------------------------------------------
# Round-robin fairness (step 7): per-call dispatch cap + cross-campaign share
# ---------------------------------------------------------------------------


def test_drive_respects_max_dispatch(driver_env):
    """max_dispatch caps a single drive; the rest defers to the next drive."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=10, chunk_size=12, requested=120,
                   concurrency_target=8)

    launched = drive_campaign("camp-1", max_dispatch=3)
    assert launched == 3
    assert {k["chunk_index"] for k in _children(client)} == {0, 1, 2}

    # A second capped drive resumes at the frontier: 3 more, still contiguous.
    launched2 = drive_campaign("camp-1", max_dispatch=3)
    assert launched2 == 3
    assert {k["chunk_index"] for k in _children(client)} == {0, 1, 2, 3, 4, 5}


def test_drive_terminal_returns_zero(driver_env):
    """A terminal / draft campaign launches nothing and reports 0."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=2, status="completed")
    assert drive_campaign("camp-1") == 0
    assert _children(client) == []


def test_tick_round_robin_shares_balance(driver_env, monkeypatch):
    """Two campaigns of one user split the shared wallet fairly, not first-come-all."""
    client, state = driver_env
    # The cron imports its service client from shared.credits (not cc), so patch
    # that seam too; the drive it calls still uses the cc seam driver_env patched.
    import shared.credits as credits
    monkeypatch.setattr(credits, "get_service_client", lambda: client)
    from cron.tick_campaigns import tick_campaigns

    _seed_campaign(client, campaign_id="camp-A", total_subjobs=8, chunk_size=12,
                   requested=96, concurrency_target=8)
    _seed_campaign(client, campaign_id="camp-B", total_subjobs=8, chunk_size=12,
                   requested=96, concurrency_target=8)
    # Fund exactly 8 chunks of the shared wallet; each campaign wants 8. Without
    # fairness the first campaign would take all 8 and starve the second.
    one_hold = cc.child_hold_usd("rfdiffusion", 12)
    state["wallet_balance"] = one_hold * 8

    summary = tick_campaigns()

    per_campaign: dict = {}
    for k in _children(client):
        per_campaign[k["campaign_id"]] = per_campaign.get(k["campaign_id"], 0) + 1
    assert per_campaign.get("camp-A") == 4
    assert per_campaign.get("camp-B") == 4
    assert summary["driven"] == 2


# ---------------------------------------------------------------------------
# 4b: pause bookkeeping (paused_at), durable email (pause_notified_at), TTL sweep
# ---------------------------------------------------------------------------


def test_pause_sets_paused_at_and_notified(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=3, chunk_size=12, requested=36)
    _patch_pause_email(monkeypatch)  # returns True -> durable notify stamps the flag
    state["wallet_balance"] = Decimal("0")

    drive_campaign("camp-1")

    row = client.store["compute_campaigns"][0]
    assert row["status"] == "paused_insufficient_funds"
    assert row.get("paused_at")          # stamped on pause (feeds the TTL)
    assert row.get("pause_notified_at")  # send confirmed -> stamped


def test_pause_leaves_notified_null_when_email_drops(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=3, chunk_size=12, requested=36)
    import shared.email as em
    monkeypatch.setattr(em, "send_campaign_paused_email", lambda **kw: False)  # dropped
    state["wallet_balance"] = Decimal("0")

    drive_campaign("camp-1")
    row = client.store["compute_campaigns"][0]
    assert row["status"] == "paused_insufficient_funds"
    assert row.get("paused_at")
    assert row.get("pause_notified_at") is None  # not confirmed -> cron will retry


def test_resume_clears_pause_bookkeeping(driver_env, monkeypatch):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=3, chunk_size=12, requested=36)
    _patch_pause_email(monkeypatch)
    one_hold = cc.child_hold_usd("rfdiffusion", 12)
    state["wallet_balance"] = one_hold  # one chunk, then pause

    drive_campaign("camp-1")
    row = client.store["compute_campaigns"][0]
    assert row["status"] == "paused_insufficient_funds" and row.get("paused_at")

    state["wallet_balance"] = one_hold * 10  # top up -> resume
    drive_campaign("camp-1")
    row = client.store["compute_campaigns"][0]
    assert row["status"] == "running"
    assert row.get("paused_at") is None
    assert row.get("pause_notified_at") is None


def test_sweep_ttl_finalizes_long_paused_with_delivery(driver_env):
    client, _ = driver_env
    row = _seed_campaign(client, total_subjobs=3, status="paused_insufficient_funds")
    row["paused_at"] = "2000-01-01T00:00:00+00:00"  # far past the 14-day TTL
    client.store["tool_jobs"].append(
        {"campaign_id": "camp-1", "chunk_index": 0, "status": "succeeded"})

    summary = cc.sweep_paused_campaigns()
    assert summary["finalized"] == 1
    assert client.store["compute_campaigns"][0]["status"] == "completed_with_failures"


def test_sweep_ttl_finalizes_empty_paused_as_cancelled(driver_env):
    client, _ = driver_env
    row = _seed_campaign(client, total_subjobs=3, status="paused_insufficient_funds")
    row["paused_at"] = "2000-01-01T00:00:00+00:00"

    summary = cc.sweep_paused_campaigns()
    assert summary["finalized"] == 1
    assert client.store["compute_campaigns"][0]["status"] == "cancelled"


def test_sweep_leaves_recent_pause_alone(driver_env):
    client, _ = driver_env
    from datetime import datetime, timezone
    row = _seed_campaign(client, total_subjobs=3, status="paused_insufficient_funds")
    row["paused_at"] = datetime.now(timezone.utc).isoformat()   # just paused
    row["pause_notified_at"] = "2026-07-06T00:00:00+00:00"      # already notified

    summary = cc.sweep_paused_campaigns()
    assert summary == {"finalized": 0, "renotified": 0}
    assert client.store["compute_campaigns"][0]["status"] == "paused_insufficient_funds"


def test_sweep_renotifies_unnotified_paused(driver_env, monkeypatch):
    client, _ = driver_env
    from datetime import datetime, timezone
    calls = _patch_pause_email(monkeypatch)
    row = _seed_campaign(client, total_subjobs=3, status="paused_insufficient_funds")
    row["paused_at"] = datetime.now(timezone.utc).isoformat()
    row["pause_notified_at"] = None  # dropped at pause time

    summary = cc.sweep_paused_campaigns()
    assert summary["renotified"] == 1
    assert len(calls) == 1
    assert client.store["compute_campaigns"][0].get("pause_notified_at")  # now stamped


# ---------------------------------------------------------------------------
# Global per-user in-flight cap (step 6)
# ---------------------------------------------------------------------------


def _seed_other_inflight(client, n, *, user_id="user-1"):
    """Seed n in-flight sub-jobs for the user from a different campaign."""
    for i in range(n):
        client.store["tool_jobs"].append({
            "id": f"other-{i}", "user_id": user_id, "campaign_id": "other",
            "chunk_index": i, "attempt": 1, "status": "running",
        })


def test_drive_respects_global_inflight_cap(driver_env):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=10, chunk_size=12, requested=120,
                   concurrency_target=8)
    # The user is already at the global cap from other campaigns' in-flight work.
    _seed_other_inflight(client, cc.GLOBAL_USER_INFLIGHT_CAP)
    drive_campaign("camp-1")
    mine = [k for k in _children(client) if k.get("campaign_id") == "camp-1"]
    assert mine == []  # no headroom -> dispatch nothing


def test_drive_global_cap_leaves_headroom(driver_env):
    client, state = driver_env
    _seed_campaign(client, total_subjobs=10, chunk_size=12, requested=120,
                   concurrency_target=8)
    # Two slots of global headroom (concurrency 8 would otherwise dispatch more).
    _seed_other_inflight(client, cc.GLOBAL_USER_INFLIGHT_CAP - 2)
    drive_campaign("camp-1")
    mine = [k for k in _children(client) if k.get("campaign_id") == "camp-1"]
    assert len(mine) == 2  # the global cap binds before the concurrency target


# ---------------------------------------------------------------------------
# Large-N efficiency: count-based driver, contiguous frontier, raised cap
# ---------------------------------------------------------------------------


def test_count_children_filters_by_campaign_and_status(driver_env):
    client, _ = driver_env
    client.store["tool_jobs"] = [
        {"id": "a", "campaign_id": "camp-1", "chunk_index": 0, "status": "running"},
        {"id": "b", "campaign_id": "camp-1", "chunk_index": 1, "status": "succeeded"},
        {"id": "c", "campaign_id": "other", "chunk_index": 0, "status": "running"},
    ]
    assert cc._count_children("camp-1") == 2
    assert cc._count_children("camp-1", ("running",)) == 1
    assert cc._count_children("camp-1", ("pending", "running")) == 1
    assert cc._count_children("other") == 1


def test_drive_dispatches_first_wave_for_large_campaign(driver_env):
    """A campaign far larger than the old 20-subjob cap dispatches exactly the
    concurrency target on the first pass, as a contiguous frontier [0, target)."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=500, chunk_size=12, requested=6000,
                   concurrency_target=16)
    drive_campaign("camp-1")
    kids = [k for k in _children(client) if k.get("campaign_id") == "camp-1"]
    assert len(kids) == 16
    assert {k["chunk_index"] for k in kids} == set(range(16))


def test_drive_repairs_legacy_gap(driver_env):
    """A NON-contiguous campaign (a hole from the old skip-past driver) is
    repaired: the missing index is filled instead of stalling forever."""
    client, state = driver_env
    _seed_campaign(client, total_subjobs=5, chunk_size=12, requested=60,
                   concurrency_target=8, status="running")
    # Pre-existing rows with a HOLE at index 2 (indices 0, 1, 3 present).
    for idx in (0, 1, 3):
        client.store["tool_jobs"].append({
            "id": f"pre-{idx}", "user_id": "user-1", "campaign_id": "camp-1",
            "chunk_index": idx, "attempt": 1, "status": "succeeded",
        })
    drive_campaign("camp-1")
    idxs = {k["chunk_index"] for k in _children(client)
            if k.get("campaign_id") == "camp-1"}
    assert idxs == {0, 1, 2, 3, 4}  # the gap at 2 (and the missing 4) were filled


def test_maybe_finalize_does_not_overwrite_cancelled(driver_env):
    """A CAS finalize must not resurrect a campaign a user just cancelled."""
    from shared.compute_campaigns import _maybe_finalize, get_campaign
    client, _ = driver_env
    _seed_campaign(client, total_subjobs=2, requested=24, status="cancelled")
    client.store["tool_jobs"] = [
        {"id": "j0", "campaign_id": "camp-1", "chunk_index": 0, "attempt": 1,
         "status": "cancelled"},
        {"id": "j1", "campaign_id": "camp-1", "chunk_index": 1, "attempt": 1,
         "status": "cancelled"},
    ]
    _maybe_finalize(get_campaign("camp-1"))
    assert _campaign_status(client) == "cancelled"
