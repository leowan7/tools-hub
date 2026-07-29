"""Unit tests for the compute-campaign module core (Phase 1).

Covers the pure chunk sizer + planner, param sanitization, the
ComputeCampaign row dataclass, and CRUD/progress against an in-memory
fake Supabase client. No live Modal or Supabase.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

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
    list_campaigns_for_target,
    list_campaigns_for_user,
    plan_chunks,
    sanitize_shared_params,
)

# The docstring above is only true with this fixture. The fake client is bound
# to ``cc.get_service_client``, but ``plan_chunks`` prices through
# ``wallet_estimates._historical_p90_seconds``, which late-imports
# ``credits.get_service_client`` -- a different module attribute the fake never
# replaces. Without the fixture every planner test SELECTs the live
# ``tool_jobs_p90`` view with the service-role key from the repo-root ``.env``,
# and the money it plans is priced off production history. Nothing here asserts
# an exact planned figure (the plan-derived assertions are all inequalities), so
# the fixture changes no expected value; it removes the production read.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


# ---------------------------------------------------------------------------
# Chunk sizer / planner (pure)
# ---------------------------------------------------------------------------


def test_chunk_size_per_tool():
    # rfdiffusion: 120 gpu_s/design, pilot cap 1800s, 0.8 util -> 12.
    assert _chunk_size_for("rfdiffusion") == 12
    # bindcraft: 1800 gpu_s/design, campaign container 36000s, 0.8 util -> 16.
    assert _chunk_size_for("bindcraft") == 16
    # boltzgen: budget-based, fixed 200-pool -> 50 delivered/job.
    assert _chunk_size_for("boltzgen") == BOLTZGEN_DESIGNS_PER_JOB
    # pxdesign: pinned to its validated 24-design pilot job (override).
    assert _chunk_size_for("pxdesign") == 24
    # rfantibody: 1800 gpu_s/design, campaign container 36000s, 0.8 util -> 16.
    assert _chunk_size_for("rfantibody") == 16


@pytest.mark.parametrize(
    "tool,requested,expected_subjobs",
    [
        ("rfdiffusion", 24, 2),
        ("rfdiffusion", 25, 3),   # 12+12+1
        ("bindcraft", 40, 3),   # 16+16+8
        ("boltzgen", 100, 2),
        ("boltzgen", 101, 3),
        ("pxdesign", 48, 2),    # 24+24
        ("pxdesign", 25, 2),    # 24+1
        ("rfantibody", 32, 2),  # 16+16
        ("rfantibody", 17, 2),  # 16+1
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
    # mpnn/af2 are real tools but not self-serve campaign design tools.
    for tool in ("mpnn", "af2", "esmfold", "nonsense"):
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


def test_bindcraft_campaign_bigger_chunk_and_session_budget():
    # bindcraft campaigns size against a larger container than the 3/chunk pilot,
    # and carry a matching session budget so the pipeline does not stop early.
    assert cc._chunk_size_for("bindcraft") == 16
    assert cc._campaign_session_inputs("bindcraft") == {"_total_budget_hours": 10.0}
    # other tools keep the default 4h session budget (no override injected).
    assert cc._campaign_session_inputs("rfdiffusion") == {}
    assert cc._campaign_session_inputs("boltzgen") == {}


def test_pxdesign_campaign_uses_validated_pilot_chunk():
    # A pxdesign campaign chunk == its validated 24-design pilot job, so no
    # bigger container and no session-budget override are needed.
    assert cc._chunk_size_for("pxdesign") == 24
    assert cc._campaign_session_inputs("pxdesign") == {}
    assert cc._campaign_container_seconds("pxdesign") == cc.preset_gpu_seconds(
        "pxdesign", "pilot"
    )
    plan = plan_chunks("pxdesign", 48)
    assert plan.total_subjobs == 2
    assert plan.chunk_size == 24
    assert plan.design_param_key == "num_designs"
    assert plan.budget_usd > 0
    with pytest.raises(ValueError):
        plan_chunks("pxdesign", 0)


def test_pxdesign_chunk_cost_priced_per_container_not_per_design():
    # One 3600s container does the whole 24-design chunk, so a chunk costs one
    # container (baseline), NOT 24x the per-design spec rate. Pricing per-design
    # would inflate the campaign budget + first-wave hold ~12x (money-safe but a
    # bogus admission gate). Mirrors boltzgen's fixed-container treatment.
    from shared.wallet_estimates import estimated_cost_for_tool
    baseline = estimated_cost_for_tool(
        None, "pxdesign", {"num_designs": 2, "preset": "pilot"}
    )
    assert cc._estimate_chunk_cost("pxdesign", 24) == baseline
    # ... and far below the naive per-design price for a full chunk.
    scaled = estimated_cost_for_tool(
        None, "pxdesign", {"num_designs": 24, "preset": "pilot"}
    )
    assert cc._estimate_chunk_cost("pxdesign", 24) < scaled / 5


def test_pxdesign_first_wave_gate_is_per_container_not_inflated():
    # The campaign START gate (first_wave_hold_usd) is worst-case one cushioned
    # per-container hold per wave. A 48-design pxdesign campaign is 2 sub-jobs,
    # so the gate is ~2 containers (~$13), NOT the ~$157 a per-design hold would
    # demand. Guards the estimate/hold parity end-to-end at the admission gate.
    plan = plan_chunks("pxdesign", 48)
    assert plan.total_subjobs == 2
    per_container = cc.child_hold_usd("pxdesign", plan.chunk_size)
    gate = cc.first_wave_hold_usd(plan)
    assert gate == cc._quantize_usd(per_container * plan.total_subjobs)
    # Well under the naive per-design gate (24x per chunk) that the pre-fix code
    # produced.
    naive_per_chunk = cc.cushioned_hold_usd(
        None, "pxdesign", {"num_designs": plan.chunk_size, "preset": "pilot"}
    )
    assert gate < naive_per_chunk


def test_rfantibody_campaign_bigger_chunk_and_session_budget():
    # rfantibody mirrors bindcraft: a 10h campaign container (36000s) sits under
    # the 23h Modal timeout, giving 16 designs/chunk with a matching session
    # budget passed through _campaign_session_inputs.
    assert cc._chunk_size_for("rfantibody") == 16
    assert cc._campaign_container_seconds("rfantibody") == 36000
    assert cc._campaign_session_inputs("rfantibody") == {"_total_budget_hours": 10.0}
    plan = plan_chunks("rfantibody", 32)
    assert plan.total_subjobs == 2
    assert plan.chunk_size == 16
    assert plan.design_param_key == "num_designs"
    assert plan.budget_usd > 0
    with pytest.raises(ValueError):
        plan_chunks("rfantibody", 0)


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
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


# The PostgREST max_rows in supabase/config.toml. Modelled here because a fake
# that hands back every row makes any paging test decorative: the code under
# test only pages because the real backend truncates, so a fake that never
# truncates passes whether the paging works or not.
_FAKE_MAX_ROWS = 1000


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._filters = []
        self._neq_filters = []
        self._in_filters = []
        self._insert_row = None
        self._update_fields = None
        self._single = False
        self._count = None
        self._head = False
        self._range = None
        self._order = None

    def select(self, *_a, **_k):
        self._count = _k.get("count")
        self._head = bool(_k.get("head", False))
        return self

    def insert(self, row):
        self._insert_row = dict(row)
        return self

    def update(self, fields):
        # Modelled because the CAS semantics live entirely in the RETURN value.
        # ``_cas_transition`` does `.update(...).eq("id").in_("status", allowed)`
        # and reads `bool(resp.data)`: PostgREST applies the UPDATE only to rows
        # matching the filters and returns exactly those rows, so an empty list
        # IS the "someone else already moved it" answer. A fake that applied the
        # write but returned all rows, or returned a truthy stub, would make
        # every CAS look like it won.
        self._update_fields = dict(fields)
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def neq(self, col, val):
        # Modelled, not accepted-and-ignored. list_campaigns_for_target excludes
        # drafts server-side, and its own except-clause swallows an
        # AttributeError into an empty list, so a fake missing this method turns
        # "the filter is broken" into "this target has no runs" -- which is the
        # exact user-visible failure the server-side filter exists to avoid.
        self._neq_filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._in_filters.append((col, {str(v) for v in vals}))
        return self

    def gte(self, *_a, **_k):
        # Date-window filter; accept-all keeps tests date-independent.
        return self

    def order(self, col, **kw):
        self._order = (col, bool(kw.get("desc", False)))
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def limit(self, *_a, **_k):
        # Deliberately a no-op beyond the clamp in execute(): PostgREST clamps
        # .limit() to max_rows exactly as it clamps an unbounded select, which
        # is why .range() is the only way past it.
        return self

    def single(self):
        self._single = True
        return self

    def _matches(self, r):
        if not all(str(r.get(c)) == str(v) for c, v in self._filters):
            return False
        if any(str(r.get(c)) == str(v) for c, v in self._neq_filters):
            return False
        return all(str(r.get(c)) in vals for c, vals in self._in_filters)

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
        if self._update_fields is not None:
            for row in matched:
                row.update(self._update_fields)
            # The matched rows only. Empty when the filters excluded everything,
            # which is how a losing CAS reports that it lost.
            return _Result(matched)
        if self._count == "exact":
            # Mirror PostgREST head+exact count: count set, data omitted on head.
            return _Result(None if self._head else matched, count=len(matched))
        if self._single:
            if not matched:
                raise RuntimeError("no rows")
            return _Result(matched[0])
        if self._order is not None:
            col, desc = self._order
            matched = sorted(matched, key=lambda r: str(r.get(col, "")), reverse=desc)
        if self._range is not None:
            start, end = self._range
            matched = matched[start:end + 1]
        # Applied last and unconditionally, as PostgREST does: a .range() wider
        # than max_rows still comes back clamped.
        return _Result(matched[:_FAKE_MAX_ROWS])


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
            user_id="u", tool="mpnn", params={}, requested_designs=10,
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


def _run_row(i, *, target_id="t-1", user_id="user-1", created="2026-07-03T00:00:00Z",
             status="running"):
    return {
        "id": f"c{i:05d}", "user_id": user_id, "target_id": target_id,
        "tool": "rfdiffusion", "preset": "pilot", "status": status,
        "name": f"run-{i}", "created_at": created,
    }


def test_list_campaigns_for_target_hides_stranded_drafts(fake_client):
    """A multi-tool launch inserts every run as a draft and funds them after,
    so a launch that fails part way leaves drafts behind. A draft was never
    funded, dispatched or billed, and there is no action the page can offer on
    it, so listing it under a heading that says "Runs" claims something false."""
    fake_client.store["compute_campaigns"] = [
        _run_row(1),
        _run_row(2, status="draft"),
        _run_row(3, status="completed"),
    ]
    runs = list_campaigns_for_target("t-1", user_id="user-1")
    assert [c.id for c in runs] == ["c00001", "c00003"]


def test_list_campaigns_for_target_can_still_see_drafts_on_request(fake_client):
    fake_client.store["compute_campaigns"] = [
        _run_row(1), _run_row(2, status="draft"),
    ]
    runs = list_campaigns_for_target("t-1", user_id="user-1", include_drafts=True)
    assert [c.id for c in runs] == ["c00001", "c00002"]


def test_list_campaigns_for_target_filters_server_side(fake_client):
    fake_client.store["compute_campaigns"] = [
        _run_row(1), _run_row(2),
        _run_row(3, target_id="t-other"),
        _run_row(4, target_id=None),
    ]
    runs = list_campaigns_for_target("t-1", user_id="user-1")
    assert [c.id for c in runs] == ["c00001", "c00002"]


def test_list_campaigns_for_target_is_owner_scoped(fake_client):
    fake_client.store["compute_campaigns"] = [
        _run_row(1, user_id="user-1"),
        _run_row(2, user_id="someone-else"),
    ]
    runs = list_campaigns_for_target("t-1", user_id="user-1")
    assert [c.id for c in runs] == ["c00001"]


def test_list_campaigns_for_target_pages_past_the_row_clamp(fake_client):
    """A target can hold more runs than PostgREST will hand back in one read.

    The clamp is invisible at the call site -- a truncated page looks exactly
    like a complete one -- so without .range() paging the target page would
    show the first 1000 runs and give no sign the rest exist.
    """
    n = 2400
    fake_client.store["compute_campaigns"] = [_run_row(i) for i in range(n)]

    runs = list_campaigns_for_target("t-1", user_id="user-1")

    assert len(runs) == n, "run list was truncated at the PostgREST row clamp"
    assert {c.id for c in runs} == {f"c{i:05d}" for i in range(n)}


def test_stranded_drafts_do_not_consume_the_page_budget(fake_client):
    """WHY the draft filter is in the query and not in memory.

    That the drafts are omitted is already covered above; this covers the cost
    of omitting them in the wrong place. The read is bounded at
    ``_MAX_TARGET_RUN_PAGES`` pages of ``_TARGET_RUN_PAGE_SIZE`` rows, and that
    bound counts ROWS RETURNED BY POSTGRES. Drop drafts locally instead and a
    target carrying enough stranded drafts spends its entire page budget on
    rows that are then thrown away, so its real runs vanish from the page with
    no error -- the failure mode the paging exists to prevent, reintroduced one
    layer up.

    Drafts are given the LOW ids deliberately: pages are ordered by ``id``, so
    this is the arrangement where an in-memory filter loses everything rather
    than merely some of it.
    """
    budget = cc._MAX_TARGET_RUN_PAGES * cc._TARGET_RUN_PAGE_SIZE
    real = 400
    assert real < cc._TARGET_RUN_PAGE_SIZE, (
        "the real runs must fit in ONE filtered page, so this test fails only "
        "because of the drafts"
    )
    fake_client.store["compute_campaigns"] = (
        [_run_row(i, status="draft") for i in range(budget)]
        + [_run_row(budget + i) for i in range(real)]
    )

    runs = list_campaigns_for_target("t-1", user_id="user-1")

    assert len(runs) == real, (
        f"{real} real runs exist but {len(runs)} came back; the drafts ate the "
        f"page budget"
    )
    assert all(c.status != "draft" for c in runs)


def test_list_campaigns_for_target_returns_newest_first(fake_client):
    """Pages are read ordered by id (stable across boundaries); the
    newest-first ordering the page renders is applied after."""
    fake_client.store["compute_campaigns"] = [
        _run_row(1, created="2026-07-01T00:00:00Z"),
        _run_row(2, created="2026-07-09T00:00:00Z"),
        _run_row(3, created="2026-07-05T00:00:00Z"),
    ]
    runs = list_campaigns_for_target("t-1", user_id="user-1")
    assert [c.id for c in runs] == ["c00002", "c00003", "c00001"]


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


def test_get_progress_counts_stays_consistent_when_total_read_fails(monkeypatch):
    # If the total COUNT fails (sentinel default), return a self-consistent
    # all-zeros dict rather than nonzero buckets over a zero total.
    def fake_count(cid, statuses=None, default=0):
        return default if statuses is None else 5  # total "fails", buckets "succeed"
    monkeypatch.setattr(cc, "_count_children", fake_count)
    counts = get_progress_counts("camp-x")
    assert counts["total"] == 0
    assert all(counts[s] == 0 for s in cc._CHILD_STATUSES)
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


def test_preauth_no_verification_when_kyc_disabled(fake_client, monkeypatch):
    """Default posture: KYC is OFF, so a large budget does NOT require
    verification (the velocity cap remains the only budget-size gate)."""
    monkeypatch.setattr(cc, "CAMPAIGN_KYC_ENABLED", False)
    _patch_wallet(monkeypatch, balance_usd="100000")  # plenty of balance
    over = cc.VERIFICATION_THRESHOLD_USD + Decimal("1")
    res = cc.campaign_preauth("u", over)
    assert res.ok and res.reason == cc.PREAUTH_OK


def test_preauth_verification_required_over_threshold_when_kyc_enabled(fake_client, monkeypatch):
    """With the KYC flag ON, a large budget requires an approved account."""
    monkeypatch.setattr(cc, "CAMPAIGN_KYC_ENABLED", True)
    _patch_wallet(monkeypatch, balance_usd="100000")  # plenty of balance
    over = cc.VERIFICATION_THRESHOLD_USD + Decimal("1")
    res = cc.campaign_preauth("u", over)
    assert not res.ok and res.reason == cc.PREAUTH_VERIFICATION


def test_preauth_verification_passes_with_override(fake_client, monkeypatch):
    monkeypatch.setattr(cc, "CAMPAIGN_KYC_ENABLED", True)
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
    """A small first wave must NOT let a large-budget campaign skip verification
    (only relevant when the KYC flag is ON)."""
    monkeypatch.setattr(cc, "CAMPAIGN_KYC_ENABLED", True)
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


# ---------------------------------------------------------------------------
# Scaling-key generalization (Phase 0): the per-chunk estimate/hold must scale
# on each tool's real wallet scaling_param, not a hardcoded "num_designs".
# ---------------------------------------------------------------------------


def test_scaling_key_is_num_designs_for_every_live_tool():
    # Every live campaign tool's wallet scaling_param is "num_designs", so the
    # generalized key resolves there and the estimate/hold stay BYTE-IDENTICAL
    # to the old hardcoded path. This locks the "no regression to the live
    # tools" guarantee.
    for tool in ("rfdiffusion", "bindcraft", "boltzgen", "pxdesign",
                 "rfantibody", "proteina"):
        assert cc._scaling_key_for(tool) == "num_designs", tool


def test_scaling_key_follows_the_tool_spec():
    # A tool whose wallet scaling_param differs resolves to that key, so its
    # per-chunk cost scales instead of falling back to the 1-unit baseline.
    assert cc._scaling_key_for("iggm") == "num_samples"       # linear, num_samples
    assert cc._scaling_key_for("af2") == "n_designs_total"     # fold batch
    # Unknown / unregistered tool falls back to the historical default.
    assert cc._scaling_key_for("does-not-exist") == "num_designs"


def test_non_num_designs_tool_prices_under_its_own_key():
    # The bug Phase 0 fixes: a linear tool whose scaling_param is not
    # "num_designs" (iggm=num_samples) was priced under a key its ToolSpec never
    # reads, so _effective_scaling_value fell back to the 1-unit baseline and the
    # per-chunk cost did NOT grow with the count. Passing the count under the
    # tool's real scaling_param scales it; the old hardcoded "num_designs" did
    # not. (iggm baseline=1, so 40 units scales ~40x.)
    from shared.wallet_estimates import estimated_cost_for_tool
    key = cc._scaling_key_for("iggm")
    assert key == "num_samples"
    scaled = estimated_cost_for_tool(None, "iggm", {key: 40, "preset": "pilot"})
    under_old_key = estimated_cost_for_tool(
        None, "iggm", {"num_designs": 40, "preset": "pilot"}
    )
    assert scaled > under_old_key
    # And the campaign hold for a linear non-num_designs tool now scales with the
    # chunk (iggm is not fixed-container).
    assert "iggm" not in cc._FIXED_CONTAINER_TOOLS
    assert cc.child_hold_usd("iggm", 40) > cc.child_hold_usd("iggm", 1)


# ---------------------------------------------------------------------------
# preauth_message (the copy a refused user actually reads)
# ---------------------------------------------------------------------------

# DERIVED from the message table, not listed. A new refusal reason is the normal
# way a new placeholder arrives, so a hardcoded tuple would let exactly the case
# the placeholder test promises to catch ship green: add
# `_PREAUTH_MESSAGES["chargeback_hold"] = "... {subject} {oops}."` and the
# parametrization would never visit it, so `{oops}` reaches the user as template
# source. Deriving means adding a reason automatically adds its coverage.
_ALL_REFUSAL_REASONS = tuple(cc._PREAUTH_MESSAGES)


def test_every_refusal_reason_constant_is_in_the_message_table():
    """The derivation above is only complete if the table is. A reason constant
    with no entry falls through to the generic sentence, which is a deliberate
    fallback -- but it must be deliberate, not an omission nobody noticed."""
    declared = {
        cc.PREAUTH_NO_WALLET,
        cc.PREAUTH_FROZEN,
        cc.PREAUTH_INSUFFICIENT,
        cc.PREAUTH_VERIFICATION,
        cc.PREAUTH_VELOCITY,
    }
    assert declared == set(_ALL_REFUSAL_REASONS)
    assert cc.PREAUTH_OK not in _ALL_REFUSAL_REASONS

# Every placeholder preauth_message substitutes. A leftover brace means the
# user was shown template source.
_PLACEHOLDERS = ("{threshold}", "{subject}", "{pauses}", "{smaller}", "{required}")


def _refused(reason, required="1", balance="0", budget="50"):
    return cc.PreauthResult(
        ok=False,
        reason=reason,
        balance_usd=Decimal(balance),
        budget_usd=Decimal(budget),
        required_usd=Decimal(required),
    )


def test_the_required_amount_rounds_up_never_down():
    """The refusal must name a figure that would actually clear the gate.

    This is the money bug the whole message exists to avoid. The gate holds a
    4dp Decimal, and "%.2f"/ROUND_HALF_EVEN on 573.6736 names $573.67 -- one
    third of a cent BELOW the amount that just refused the user. Topping up to
    exactly the figure in the sentence gets you refused again by the same
    sentence. A required amount is a ceiling or it is nothing.
    """
    msg = cc.preauth_message(_refused(cc.PREAUTH_INSUFFICIENT, required="573.6736"))
    assert "$573.68" in msg
    assert "573.67" not in msg


def test_the_required_amount_is_never_rounded_up_past_a_whole_cent():
    """ROUND_CEILING, not "add a cent". An exact 2dp figure must not inflate:
    quoting more than the gate needs is its own wrong number."""
    msg = cc.preauth_message(_refused(cc.PREAUTH_INSUFFICIENT, required="573.6700"))
    assert "$573.67" in msg
    assert "573.68" not in msg


def test_a_zero_required_amount_still_renders_as_money():
    """Decimal("0") is falsy. Testing `if required` instead of
    `if required is not None` swaps a real $0.00 gate for the fallback wording,
    which reads as though the amount were unknown.

    Asserted on the substituted CONTEXT, not on the absence of the fallback
    phrase: the fallback is the words "the first batch", which the INSUFFICIENT
    template already contains verbatim ("does not cover the first batch of
    ..."), so a plain negative substring assertion can never fail here.
    """
    msg = cc.preauth_message(_refused(cc.PREAUTH_INSUFFICIENT, required="0"))
    assert "(about $0.00 to start)" in msg


def test_a_missing_required_amount_falls_back_to_words():
    """Unreachable from ``campaign_preauth`` (``required_usd`` defaults to
    ``Decimal("0")``), so this pins the guard, not a live path. Note the
    fallback reads oddly in this particular template -- "does not cover the
    first batch of this campaign (about the first batch to start)" -- which is
    tolerable precisely because nothing in production can produce it."""
    pre = SimpleNamespace(
        ok=False, reason=cc.PREAUTH_INSUFFICIENT,
        balance_usd=Decimal("0"), budget_usd=Decimal("50"), required_usd=None,
    )
    msg = cc.preauth_message(pre)
    assert "(about the first batch to start)" in msg
    assert "$" not in msg


@pytest.mark.parametrize("reason", _ALL_REFUSAL_REASONS)
@pytest.mark.parametrize("count", [1, 2, 7])
def test_no_placeholder_survives_for_any_reason_or_count(reason, count):
    """Red if a new placeholder is added to the table without a substitution,
    which would ship template source into a user-facing refusal."""
    msg = cc.preauth_message(_refused(reason), count=count)
    for token in _PLACEHOLDERS:
        assert token not in msg, f"{token} unsubstituted for {reason}/{count}"
    assert "{" not in msg and "}" not in msg


# Which refusals are count-sensitive, DECLARED rather than derived from the
# template. Reading the branch off the very table under assertion means stripping
# {subject} from the INSUFFICIENT copy silently flips the test into the `else`
# arm, where `one == many` then holds -- so the regression asserts itself away.
_COUNT_SENSITIVE = {
    cc.PREAUTH_NO_WALLET: False,     # "Your wallet is unavailable." No placeholders.
    cc.PREAUTH_FROZEN: False,        # "Your wallet is on hold." No placeholders.
    cc.PREAUTH_INSUFFICIENT: True,   # {subject}, {pauses}
    cc.PREAUTH_VERIFICATION: False,  # {threshold} only, which does not vary
    cc.PREAUTH_VELOCITY: True,       # {smaller}
}


def test_every_refusal_reason_declares_whether_its_copy_varies_by_count():
    """So a new reason cannot slip past the parametrization below by defaulting
    to "identical is fine"."""
    assert set(_COUNT_SENSITIVE) == set(_ALL_REFUSAL_REASONS)


@pytest.mark.parametrize("reason", _ALL_REFUSAL_REASONS)
def test_singular_and_plural_copy_differ_where_the_subject_appears(reason):
    """A 7-tool launch told "this campaign cannot start" misdescribes what was
    refused and points at the wrong remedy: with several tools selected,
    dropping one is usually cheaper than topping up."""
    one = cc.preauth_message(_refused(reason), count=1)
    many = cc.preauth_message(_refused(reason), count=7)
    if _COUNT_SENSITIVE[reason]:
        assert one != many, f"{reason} renders identically at count 1 and 7"
    else:
        assert one == many, f"{reason} is not supposed to vary by count"


def test_the_plural_subject_names_the_run_count():
    msg = cc.preauth_message(_refused(cc.PREAUTH_INSUFFICIENT), count=7)
    assert "these 7 runs" in msg
    assert "this campaign" not in msg


def test_the_singular_subject_says_campaign():
    msg = cc.preauth_message(_refused(cc.PREAUTH_INSUFFICIENT), count=1)
    assert "this campaign" in msg
    assert "runs" not in msg.split("Top up")[0]


def test_the_velocity_refusal_suggests_dropping_tools_when_plural():
    """The two remedies are different actions, not a wording change: one tells
    you to shrink a single campaign, the other to deselect a tool."""
    assert "fewer tools" in cc.preauth_message(
        _refused(cc.PREAUTH_VELOCITY), count=3
    )
    assert "a smaller campaign" in cc.preauth_message(
        _refused(cc.PREAUTH_VELOCITY), count=1
    )


def test_the_verification_refusal_names_the_real_threshold():
    msg = cc.preauth_message(_refused(cc.PREAUTH_VERIFICATION))
    assert f"${cc.VERIFICATION_THRESHOLD_USD}" in msg


@pytest.mark.parametrize("count,expected", [(1, "This campaign"), (2, "These runs")])
def test_an_unknown_reason_still_produces_a_sentence(count, expected):
    msg = cc.preauth_message(_refused("some_reason_added_later"), count=count)
    assert msg.startswith(expected)
    assert msg.endswith(".")


# ---------------------------------------------------------------------------
# fund_campaign (the launch route branches on this bool)
# ---------------------------------------------------------------------------


def _seed_campaign(fake_client, status="draft", campaign_id="camp-fund"):
    fake_client.store.setdefault("compute_campaigns", []).append({
        "id": campaign_id, "user_id": "u-1", "tool": "rfdiffusion",
        "preset": "pilot", "status": status, "requested_designs": 12,
        "chunk_size": 12, "total_subjobs": 1, "budget_usd": "4.02",
        "reserved_usd": "0", "spent_usd": "0", "refunded_usd": "0",
        "created_at": "2026-07-03T00:00:00Z",
    })
    return campaign_id


def _status_of(fake_client, campaign_id):
    for row in fake_client.store.get("compute_campaigns", []):
        if row["id"] == campaign_id:
            return row["status"]
    raise AssertionError(f"{campaign_id} not in the fake store")


def test_fund_campaign_reports_true_and_moves_the_row(fake_client):
    """The whole point of the bool. ``drive_campaign`` early-returns on a
    draft, so a fund that silently failed leaves a campaign the user believes
    is running parked forever. The launch route reads this to decide whether to
    drive, and to count stalled runs (audit item A12)."""
    cid = _seed_campaign(fake_client, status="draft")
    assert cc.fund_campaign(cid) is True
    assert _status_of(fake_client, cid) == "funded"


def test_fund_campaign_stamps_confirmed_at(fake_client):
    cid = _seed_campaign(fake_client, status="draft")
    cc.fund_campaign(cid)
    row = next(r for r in fake_client.store["compute_campaigns"] if r["id"] == cid)
    assert row.get("confirmed_at")


@pytest.mark.parametrize(
    "status", ["funded", "running", "completed", "cancelled", "failed"]
)
def test_fund_campaign_refuses_a_row_that_is_not_draft(fake_client, status):
    """CAS on ``draft``, so this can no longer rewind a campaign that has
    already progressed. The pre-Phase-2 implementation used an unconditional
    UPDATE through ``_update_campaign``, which both rewound a running campaign
    to ``funded`` and returned None either way."""
    cid = _seed_campaign(fake_client, status=status)
    assert cc.fund_campaign(cid) is False
    assert _status_of(fake_client, cid) == status


def test_fund_campaign_reports_false_for_an_unknown_id(fake_client):
    _seed_campaign(fake_client, status="draft")
    assert cc.fund_campaign("no-such-campaign") is False


def test_fund_campaign_reports_false_with_no_client(monkeypatch):
    """The route funds N rows in a loop; a missing client must read as "did not
    start", never as success."""
    monkeypatch.setattr(cc, "get_service_client", lambda: None)
    assert cc.fund_campaign("camp-1") is False


def test_only_the_first_of_two_concurrent_funds_wins(fake_client):
    """Exactly the property the CAS exists for: the second caller sees the row
    already out of draft and reports False rather than re-funding it."""
    cid = _seed_campaign(fake_client, status="draft")
    assert cc.fund_campaign(cid) is True
    assert cc.fund_campaign(cid) is False


@pytest.mark.parametrize("required,expected", [
    (573.6736, "$573.68"),          # float
    (574, "$574.00"),               # int
    ("573.6736", "$573.68"),        # str
])
def test_the_required_amount_survives_a_non_decimal(required, expected):
    """A refusal must never become a 500.

    `PreauthResult` is a plain frozen dataclass with no coercion, so nothing
    stops a caller passing a float; `float.quantize` does not exist. This is the
    one function whose entire job is to explain a refusal to a user, and an
    AttributeError here replaces "top up $573.68" with an error page. Rounding
    still goes UP for every input type.
    """
    pre = SimpleNamespace(
        ok=False, reason=cc.PREAUTH_INSUFFICIENT,
        balance_usd=Decimal("0"), budget_usd=Decimal("50"),
        required_usd=required,
    )
    msg = cc.preauth_message(pre)
    assert expected in msg


@pytest.mark.parametrize("bad", ["abc", float("inf"), float("nan"), None, object()])
def test_an_unrenderable_required_amount_falls_back_instead_of_500ing(bad):
    """A refusal must never become an error page.

    `Decimal(str(x))` raises InvalidOperation for a non-numeric string, for inf,
    and for a huge exponent, and TypeError for an arbitrary object. All of those
    reach here only through a future caller, but this function's entire job is to
    explain a refusal, so the failure mode has to be the fallback wording rather
    than a 500. NaN is included deliberately: it does not raise, it renders, and
    "about $NaN to start" is not an acceptable thing to show a user.
    """
    pre = SimpleNamespace(
        ok=False, reason=cc.PREAUTH_INSUFFICIENT,
        balance_usd=Decimal("0"), budget_usd=Decimal("50"), required_usd=bad,
    )
    msg = cc.preauth_message(pre)
    assert "(about the first batch to start)" in msg
    assert "NaN" not in msg and "$" not in msg
