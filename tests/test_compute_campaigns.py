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
    # boltzgen drops 'budget', keeps 'num_designs' irrelevant here.
    out2 = sanitize_shared_params("boltzgen", {"budget": 50, "protocol": "nanobody-anything"})
    assert out2 == {"protocol": "nanobody-anything"}


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
    # remaining = budget - reserved - spent = 4.02 - 1.75 - 0.50 = 1.77
    assert d["remaining_usd"] == pytest.approx(1.77)
    assert d["status"] == "running"


def test_campaign_remaining_never_negative():
    row = {
        "id": "c", "user_id": "u", "tool": "rfdiffusion", "preset": "pilot",
        "status": "completed", "requested_designs": 12, "chunk_size": 12,
        "total_subjobs": 1, "budget_usd": "1.00", "reserved_usd": "0",
        "spent_usd": "5.00", "refunded_usd": "0",
    }
    assert ComputeCampaign.from_row(row).to_dict()["remaining_usd"] == 0.0


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
