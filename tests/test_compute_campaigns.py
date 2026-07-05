"""Unit tests for the compute-campaign module core (Phase 1).

Covers the pure chunk sizer + planner, param sanitization, the
ComputeCampaign row dataclass, and CRUD/progress against an in-memory
fake Supabase client. No live Modal or Supabase.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

import shared.compute_campaigns as cc
from shared.compute_campaigns import (
    BOLTZGEN_DESIGNS_PER_JOB,
    MAX_SUBJOBS_PER_CAMPAIGN,
    ComputeCampaign,
    _chunk_size_for,
    create_campaign,
    get_campaign,
    get_progress_counts,
    list_campaigns_for_user,
    plan_chunks,
    sanitize_shared_params,
)


# ---------------------------------------------------------------------------
# Chunk sizer / planner (pure)
# ---------------------------------------------------------------------------


def test_chunk_size_per_tool():
    # rfdiffusion: 120 gpu_s/design, pilot cap 1800s, 0.8 util -> 12.
    assert _chunk_size_for("rfdiffusion") == 12
    # bindcraft: 1800 gpu_s/design, pilot cap 7200s, 0.8 util -> 3.
    assert _chunk_size_for("bindcraft") == 3
    # boltzgen: budget-based, fixed 200-pool -> 50 delivered/job.
    assert _chunk_size_for("boltzgen") == BOLTZGEN_DESIGNS_PER_JOB


@pytest.mark.parametrize(
    "tool,requested,expected_subjobs",
    [
        ("rfdiffusion", 24, 2),
        ("rfdiffusion", 25, 3),   # 12+12+1
        ("bindcraft", 9, 3),
        ("boltzgen", 100, 2),
        ("boltzgen", 101, 3),
    ],
)
def test_plan_chunks_counts(tool, requested, expected_subjobs):
    plan = plan_chunks(tool, requested)
    assert plan.total_subjobs == expected_subjobs
    # Per-chunk design counts sum back to the request, last chunk smaller.
    per = [plan.designs_for_chunk(i) for i in range(plan.total_subjobs)]
    assert sum(per) == requested
    assert all(d > 0 for d in per)
    assert plan.budget_usd > 0


def test_plan_chunks_budget_scales_with_subjobs():
    small = plan_chunks("rfdiffusion", 12)
    big = plan_chunks("rfdiffusion", 120)
    assert big.total_subjobs > small.total_subjobs
    assert big.budget_usd > small.budget_usd


def test_plan_chunks_rejects_unsupported_tool():
    for tool in ("rfantibody", "pxdesign", "mpnn", "nonsense"):
        with pytest.raises(ValueError):
            plan_chunks(tool, 10)


def test_plan_chunks_rejects_over_cap():
    # rfdiffusion chunk_size 12; over MAX_SUBJOBS * 12 designs must raise.
    over = MAX_SUBJOBS_PER_CAMPAIGN * 12 + 1
    with pytest.raises(ValueError) as exc:
        plan_chunks("rfdiffusion", over)
    assert "sub-jobs" in str(exc.value)


def test_plan_chunks_rejects_bad_count():
    for bad in (0, -5):
        with pytest.raises(ValueError):
            plan_chunks("rfdiffusion", bad)
    with pytest.raises(ValueError):
        plan_chunks("rfdiffusion", "not-a-number")


def test_boltzgen_design_key_is_budget():
    assert plan_chunks("boltzgen", 50).design_param_key == "budget"
    assert plan_chunks("rfdiffusion", 12).design_param_key == "num_designs"


def test_sanitize_shared_params_strips_private_and_design_keys():
    out = sanitize_shared_params(
        "rfdiffusion",
        {
            "target_chain": "A",
            "hotspot_residues": [10, 20],
            "num_designs": 12,       # design key -> dropped
            "preset": "pilot",        # dropped
            "_workspace": {"x": 1},   # private -> dropped
        },
    )
    assert out == {"target_chain": "A", "hotspot_residues": [10, 20]}
    # boltzgen drops its own design key 'budget'.
    out2 = sanitize_shared_params("boltzgen", {"budget": 50, "protocol": "nanobody-anything"})
    assert out2 == {"protocol": "nanobody-anything"}
    # Cross-tool hardening: a stray num_designs on a boltzgen campaign is
    # ALSO stripped (else it would inflate boltzgen's num_designs-scaled cap).
    out3 = sanitize_shared_params("boltzgen", {"budget": 50, "num_designs": 99999, "protocol": "x"})
    assert out3 == {"protocol": "x"}


# ---------------------------------------------------------------------------
# ComputeCampaign dataclass
# ---------------------------------------------------------------------------


def test_campaign_from_row_and_to_dict_roundtrip():
    row = {
        "id": "camp-1",
        "user_id": "user-1",
        "tool": "rfdiffusion",
        "preset": "pilot",
        "status": "running",
        "requested_designs": 24,
        "chunk_size": 12,
        "total_subjobs": 2,
        "concurrency_target": 20,
        "max_attempts": 2,
        "budget_usd": "4.02",
        "reserved_usd": "1.75",
        "spent_usd": "0.50",
        "refunded_usd": "0",
        "params": {"target_chain": "A"},
        "name": "HER2 run",
        "target_name": "HER2",
    }
    camp = ComputeCampaign.from_row(row)
    assert camp.budget_usd == Decimal("4.02")
    assert camp.reserved_usd == Decimal("1.75")
    d = camp.to_dict()
    assert d["budget_usd"] == pytest.approx(4.02)
    assert d["status"] == "running"
    # Advisory spend fields are intentionally omitted in Phase 1 (emitting a
    # flat $0 spent while a campaign bills would mislead).
    assert "remaining_usd" not in d and "spent_usd" not in d


def test_to_dict_omits_advisory_money_fields():
    row = {
        "id": "c", "user_id": "u", "tool": "rfdiffusion", "preset": "pilot",
        "status": "completed", "requested_designs": 12, "chunk_size": 12,
        "total_subjobs": 1, "budget_usd": "1.00", "reserved_usd": "0",
        "spent_usd": "5.00", "refunded_usd": "0",
    }
    d = ComputeCampaign.from_row(row).to_dict()
    assert d["budget_usd"] == pytest.approx(1.00)
    for k in ("spent_usd", "reserved_usd", "refunded_usd", "remaining_usd"):
        assert k not in d


# ---------------------------------------------------------------------------
# Fake Supabase client for CRUD
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._filters = []
        self._insert_row = None
        self._single = False

    def select(self, *_a, **_k):
        return self

    def insert(self, row):
        self._insert_row = dict(row)
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def gte(self, *_a, **_k):
        # Date-window filter; accept-all keeps tests date-independent.
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def single(self):
        self._single = True
        return self

    def _matches(self, r):
        return all(str(r.get(c)) == str(v) for c, v in self._filters)

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._insert_row is not None:
            row = self._insert_row
            row.setdefault("id", str(uuid.uuid4()))
            for k in ("reserved_usd", "spent_usd", "refunded_usd"):
                row.setdefault(k, 0)
            row.setdefault("created_at", "2026-07-03T00:00:00Z")
            rows.append(row)
            return _Result([row])
        matched = [r for r in rows if self._matches(r)]
        if self._single:
            if not matched:
                raise RuntimeError("no rows")
            return _Result(matched[0])
        return _Result(matched)


class _Client:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _Query(self.store, name)


@pytest.fixture
def fake_client(monkeypatch):
    client = _Client()
    monkeypatch.setattr(cc, "get_service_client", lambda: client)
    return client


def test_create_and_get_campaign(fake_client):
    camp = create_campaign(
        user_id="user-1",
        tool="rfdiffusion",
        params={"target_chain": "A", "num_designs": 12, "_workspace": {"x": 1}},
        requested_designs=24,
        name="HER2 run",
        target_name="HER2",
    )
    assert camp is not None
    assert camp.status == "draft"
    assert camp.tool == "rfdiffusion"
    assert camp.total_subjobs == 2
    assert camp.budget_usd > 0
    # params sanitized on the way in.
    assert "num_designs" not in camp.params
    assert "_workspace" not in camp.params
    assert camp.params == {"target_chain": "A"}

    got = get_campaign(camp.id, user_id="user-1")
    assert got is not None and got.id == camp.id
    # owner scope enforced.
    assert get_campaign(camp.id, user_id="someone-else") is None


def test_create_campaign_raises_on_unsupported(fake_client):
    with pytest.raises(ValueError):
        create_campaign(
            user_id="u", tool="rfantibody", params={}, requested_designs=10,
        )


def test_list_campaigns_for_user(fake_client):
    for _ in range(3):
        create_campaign(
            user_id="user-1", tool="boltzgen", params={"protocol": "protein-anything"},
            requested_designs=50,
        )
    create_campaign(
        user_id="other", tool="boltzgen", params={}, requested_designs=50,
    )
    mine = list_campaigns_for_user("user-1")
    assert len(mine) == 3
    assert all(c.user_id == "user-1" for c in mine)


def test_get_progress_counts(fake_client):
    cid = "camp-x"
    fake_client.store["tool_jobs"] = [
        {"campaign_id": cid, "status": "succeeded"},
        {"campaign_id": cid, "status": "succeeded"},
        {"campaign_id": cid, "status": "running"},
        {"campaign_id": cid, "status": "failed"},
        {"campaign_id": "other", "status": "succeeded"},  # not counted
    ]
    counts = get_progress_counts(cid)
    assert counts["total"] == 4
    assert counts["succeeded"] == 2
    assert counts["running"] == 1
    assert counts["failed"] == 1
    assert counts["pending"] == 0


def test_module_helpers_return_empty_without_client(monkeypatch):
    monkeypatch.setattr(cc, "get_service_client", lambda: None)
    assert list_campaigns_for_user("u") == []
    assert get_campaign("x") is None
    assert get_progress_counts("x")["total"] == 0
    assert create_campaign(user_id="u", tool="rfdiffusion", params={}, requested_designs=12) is None


# ---------------------------------------------------------------------------
# Billing: prepaid pre-auth + estimate + admission
# ---------------------------------------------------------------------------


def _patch_wallet(monkeypatch, **fields):
    wallet = {
        "balance_usd": fields.get("balance_usd", "1000"),
        "wallet_frozen": fields.get("wallet_frozen", False),
        "per_job_cap_override_usd": fields.get("per_job_cap_override_usd"),
    }
    import shared.wallet as w
    monkeypatch.setattr(w, "get_or_create_wallet", lambda _uid: wallet)
    return wallet


def test_preauth_ok(fake_client, monkeypatch):
    _patch_wallet(monkeypatch, balance_usd="100")
    res = cc.campaign_preauth("u", Decimal("50"))
    assert res.ok and res.reason == cc.PREAUTH_OK


def test_preauth_insufficient_balance(fake_client, monkeypatch):
    _patch_wallet(monkeypatch, balance_usd="10")
    res = cc.campaign_preauth("u", Decimal("50"))
    assert not res.ok and res.reason == cc.PREAUTH_INSUFFICIENT


def test_preauth_frozen(fake_client, monkeypatch):
    _patch_wallet(monkeypatch, balance_usd="1000", wallet_frozen=True)
    res = cc.campaign_preauth("u", Decimal("50"))
    assert not res.ok and res.reason == cc.PREAUTH_FROZEN


def test_preauth_verification_required_over_threshold(fake_client, monkeypatch):
    _patch_wallet(monkeypatch, balance_usd="100000")  # plenty of balance
    over = cc.VERIFICATION_THRESHOLD_USD + Decimal("1")
    res = cc.campaign_preauth("u", over)
    assert not res.ok and res.reason == cc.PREAUTH_VERIFICATION


def test_preauth_verification_passes_with_override(fake_client, monkeypatch):
    over = cc.VERIFICATION_THRESHOLD_USD + Decimal("1000")
    _patch_wallet(monkeypatch, balance_usd="100000", per_job_cap_override_usd=str(over))
    res = cc.campaign_preauth("u", over)
    assert res.ok and res.reason == cc.PREAUTH_OK


def test_preauth_velocity_cap(fake_client, monkeypatch):
    _patch_wallet(monkeypatch, balance_usd="100000")
    monkeypatch.setattr(cc, "DAILY_CAMPAIGN_CAP_USD", Decimal("30"))
    # Seed a funded campaign today worth $20.
    fake_client.store["compute_campaigns"] = [
        {"user_id": "u", "status": "funded", "budget_usd": "20",
         "created_at": "2026-07-03T00:00:00Z"},
    ]
    res = cc.campaign_preauth("u", Decimal("15"))  # 20 + 15 = 35 > 30
    assert not res.ok and res.reason == cc.PREAUTH_VELOCITY


def test_preauth_first_wave_starts_below_full_budget(fake_client, monkeypatch):
    """Fund-and-drain: a balance covering the first wave starts the campaign
    even when it does not cover the full forecast budget."""
    _patch_wallet(monkeypatch, balance_usd="25")
    res = cc.campaign_preauth("u", Decimal("100"), first_wave_usd=Decimal("20"))
    assert res.ok and res.reason == cc.PREAUTH_OK
    assert res.required_usd == Decimal("20")
    # Legacy (no first-wave arg) gates on the full budget -> insufficient here.
    res_legacy = cc.campaign_preauth("u", Decimal("100"))
    assert not res_legacy.ok and res_legacy.reason == cc.PREAUTH_INSUFFICIENT


def test_preauth_insufficient_first_wave(fake_client, monkeypatch):
    _patch_wallet(monkeypatch, balance_usd="10")
    res = cc.campaign_preauth("u", Decimal("100"), first_wave_usd=Decimal("20"))
    assert not res.ok and res.reason == cc.PREAUTH_INSUFFICIENT
    assert res.required_usd == Decimal("20")


def test_preauth_small_first_wave_does_not_bypass_verification(fake_client, monkeypatch):
    """A small first wave must NOT let a large-budget campaign skip verification."""
    _patch_wallet(monkeypatch, balance_usd="100000")
    over = cc.VERIFICATION_THRESHOLD_USD + Decimal("1")
    res = cc.campaign_preauth("u", over, first_wave_usd=Decimal("20"))
    assert not res.ok and res.reason == cc.PREAUTH_VERIFICATION


def test_first_wave_hold_usd_bounds_by_concurrency():
    cs = cc._chunk_size_for("rfdiffusion")
    per_chunk = cc.child_hold_usd("rfdiffusion", cs)
    conc = cc.DEFAULT_CONCURRENCY_TARGET
    # More sub-jobs than the concurrency target: the first wave holds only
    # `conc` chunks' worth, not the whole campaign.
    n = conc + 4
    plan_big = cc.plan_chunks("rfdiffusion", cs * n)
    assert plan_big.total_subjobs == n
    assert cc.first_wave_hold_usd(plan_big) == cc._quantize_usd(per_chunk * conc)
    assert cc.first_wave_hold_usd(plan_big) < per_chunk * n  # not the whole thing
    # 1 sub-job < concurrency: the first wave is that single chunk.
    plan_small = cc.plan_chunks("rfdiffusion", cs)
    assert plan_small.total_subjobs == 1
    assert cc.first_wave_hold_usd(plan_small) == cc._quantize_usd(per_chunk)


def test_drive_campaign_async_spawns_daemon_and_drives(monkeypatch):
    """The first-wave kick runs off the request path in a daemon thread."""
    calls = []
    monkeypatch.setattr(cc, "drive_campaign", lambda cid: calls.append(cid))
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target
            captured["name"] = name
            captured["daemon"] = daemon

        def start(self):
            self._target()  # run inline so the test is deterministic

    import threading
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    cc.drive_campaign_async("camp-async")
    assert calls == ["camp-async"]
    assert captured["daemon"] is True
    assert captured["name"].startswith("campaign-drive-")


def test_campaign_spend_today_excludes_draft_and_cancelled(fake_client):
    fake_client.store["compute_campaigns"] = [
        {"user_id": "u", "status": "funded", "budget_usd": "10", "created_at": "2026-07-03T00:00:00Z"},
        {"user_id": "u", "status": "running", "budget_usd": "5", "created_at": "2026-07-03T00:00:00Z"},
        {"user_id": "u", "status": "draft", "budget_usd": "99", "created_at": "2026-07-03T00:00:00Z"},
        {"user_id": "u", "status": "cancelled", "budget_usd": "99", "created_at": "2026-07-03T00:00:00Z"},
    ]
    assert cc._campaign_spend_today("u") == Decimal("15")


def test_estimate_child_cost_boltzgen_flat_others_scale():
    # boltzgen: budget does not change per-job GPU cost -> flat.
    assert cc.estimate_child_cost("boltzgen", 5) == cc.estimate_child_cost("boltzgen", 50)
    # rfdiffusion: cost scales with the design count.
    assert cc.estimate_child_cost("rfdiffusion", 24) > cc.estimate_child_cost("rfdiffusion", 12)


def _campaign(total_subjobs=4, concurrency_target=2):
    return ComputeCampaign.from_row({
        "id": "c", "user_id": "u", "tool": "rfdiffusion", "preset": "pilot",
        "status": "running", "requested_designs": total_subjobs * 12,
        "chunk_size": 12, "total_subjobs": total_subjobs,
        "concurrency_target": concurrency_target,
        "budget_usd": "10", "reserved_usd": "0", "spent_usd": "0", "refunded_usd": "0",
    })


def test_can_dispatch_more():
    camp = _campaign(total_subjobs=4, concurrency_target=2)
    # Room: 0 dispatched, nothing in flight.
    assert cc.can_dispatch_more(camp, {"pending": 0, "running": 0}, 0) is True
    # Concurrency saturated.
    assert cc.can_dispatch_more(camp, {"pending": 1, "running": 1}, 2) is False
    # All chunks dispatched.
    assert cc.can_dispatch_more(camp, {"pending": 0, "running": 0}, 4) is False
    # Room again after some finished (2 dispatched, 1 in flight, target 2).
    assert cc.can_dispatch_more(camp, {"pending": 0, "running": 1}, 2) is True
