"""Unit tests for the compute-campaign module core (Phase 1).

Covers the pure chunk sizer + planner, param sanitization, the
ComputeCampaign row dataclass, and CRUD/progress against an in-memory
fake Supabase client. No live Modal or Supabase.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import shared.compute_campaigns as cc
from shared.compute_campaigns import (
    BOLTZGEN_DESIGNS_PER_JOB,
    CAMPAIGN_READ_ABSENT,
    CAMPAIGN_READ_OK,
    CAMPAIGN_READ_UNAVAILABLE,
    MAX_SUBJOBS_PER_CAMPAIGN,
    CampaignRead,
    ComputeCampaign,
    _chunk_size_for,
    create_campaign,
    get_campaign,
    get_progress_counts,
    list_campaigns_for_target,
    list_campaigns_for_user,
    plan_chunks,
    read_campaign,
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
        self._lt_filters = []
        self._is_null = []
        self._insert_row = None
        self._update_fields = None
        self._single = False
        self._count = None
        self._head = False
        self._range = None
        self._limit = None
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

    def lt(self, col, val):
        # Modelled, not accepted-and-ignored, for the same reason as neq: BOTH
        # queries in sweep_paused_campaigns sit inside a try whose bare except
        # turns an AttributeError into an empty list. A fake missing .lt() or
        # .is_() therefore reports "nothing stale, nothing to renotify" and the
        # sweep's whole body never runs, so a test using THAT fake passes while
        # exercising nothing. That is how this file read before these were
        # added. (tests/test_compute_campaigns_driver.py has its own fake, which
        # already models both, and its sweep tests did exercise the body.)
        self._lt_filters.append((col, val))
        return self

    def is_(self, col, val):
        # Accepts both spellings of the null predicate; this module passes the
        # PostgREST string, shared/idempotency.py passes None.
        assert val is None or val == "null", (
            f"is_({col!r}, {val!r}) is not a null predicate"
        )
        self._is_null.append(col)
        return self

    def order(self, col, **kw):
        self._order = (col, bool(kw.get("desc", False)))
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def limit(self, n):
        # RECORDED AND APPLIED in execute(). max_rows is an UPPER BOUND on what
        # PostgREST returns and .limit() cannot lift it -- asking for more does
        # not get you more, which is why .range() paging is the only way past it
        # -- but a limit BELOW it is honoured, and this fake was a no-op that
        # returned every matching row whatever was asked for. That made it MORE
        # PERMISSIVE than the backend on an axis nothing here was checking.
        #
        # WHAT THIS DOES NOT BUY, stated because the obvious claim is false and
        # was checked by mutation before this comment was written: it does not
        # make `read_campaign`'s absent case testable. That read is an
        # `.eq("id", ...)`, which matches at most one row, so `.limit(1)` removes
        # nothing and the empty list comes from the FILTER. Reverting this method
        # to a no-op leaves EVERY test in this file green. The `.single()`
        # mutation is caught by the fake's `single()` raising on zero rows, which
        # is what models the real distinction.
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def _matches(self, r):
        if not all(str(r.get(c)) == str(v) for c, v in self._filters):
            return False
        for col, val in self._neq_filters:
            current = r.get(col)
            # PostgREST renders this as `col <> val`, which evaluates to NULL
            # (not true) when the column is NULL, so the row is DROPPED. Python
            # `!=` KEEPS it, which is the opposite answer, and it is the answer
            # that decides whether a malformed row can reach a caller at all.
            if current is None or str(current) == str(val):
                return False
        for col in self._is_null:
            if r.get(col) is not None:
                return False
        for col, val in self._lt_filters:
            current = r.get(col)
            if current is None or not str(current) < str(val):
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
        elif self._limit is not None:
            matched = matched[:min(self._limit, _FAKE_MAX_ROWS)]
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


# ---------------------------------------------------------------------------
# read_campaign: the three-outcome read (register item A90)
#
# `get_campaign` answers None for a campaign that is not there, one that is not
# the caller's, and a read that never completed. The lab-handoff gate in
# blueprints/lab_projects.py has to act differently on the last of those -- it
# refuses the submission and says so, instead of bouncing the user to an
# unrelated list in silence -- so the difference has to survive the read.
# ---------------------------------------------------------------------------


class _RaisingQuery:
    """Builds like any other query and fails at execute().

    Models the fault the third outcome exists for: PostgREST reachable enough to
    accept a query and not to answer it. Every builder method returns self via
    ``__getattr__`` so the fake never has to track which ones the code under test
    happens to call; ``execute`` is defined on the class, so it wins the lookup.
    """

    def __getattr__(self, _name):
        return lambda *_a, **_k: self

    def execute(self):
        raise RuntimeError("PostgREST timed out")


class _RaisingClient:
    def table(self, _name):
        return _RaisingQuery()


@pytest.fixture
def raising_client(monkeypatch):
    client = _RaisingClient()
    monkeypatch.setattr(cc, "get_service_client", lambda: client)
    return client


def _a_campaign(fake_client, user_id="user-1"):
    camp = create_campaign(
        user_id=user_id, tool="rfdiffusion", params={"target_chain": "A"},
        requested_designs=12,
    )
    assert camp is not None, "fixture assumption: the insert worked"
    return camp


def test_read_campaign_reports_ok_for_a_row_that_is_there(fake_client):
    camp = _a_campaign(fake_client)
    read = read_campaign(camp.id, user_id="user-1")
    assert read.outcome == CAMPAIGN_READ_OK
    assert read.campaign is not None and read.campaign.id == camp.id
    assert read.unavailable is False


def test_read_campaign_reports_absent_when_the_read_matched_no_row(fake_client):
    """A read that COMPLETED and found nothing. The whole point of `.limit(1)`:
    under `.single()` this raises and is indistinguishable from the timeout in
    the next test, which is the defect A90 filed."""
    read = read_campaign(str(uuid.uuid4()), user_id="user-1")
    assert read.outcome == CAMPAIGN_READ_ABSENT
    assert read.campaign is None
    assert read.unavailable is False, (
        "an absent campaign is a verdict about the campaign, not about the "
        "database"
    )


def test_read_campaign_reports_unavailable_when_the_query_raises(raising_client):
    read = read_campaign(str(uuid.uuid4()), user_id="user-1")
    assert read.outcome == CAMPAIGN_READ_UNAVAILABLE
    assert read.campaign is None
    assert read.unavailable is True


def test_read_campaign_reports_unavailable_with_no_service_client(monkeypatch):
    """No client is not "no campaign". `get_campaign` cannot say so."""
    monkeypatch.setattr(cc, "get_service_client", lambda: None)
    read = read_campaign(str(uuid.uuid4()), user_id="user-1")
    assert read.outcome == CAMPAIGN_READ_UNAVAILABLE
    assert read.unavailable is True


def test_absent_and_unavailable_are_distinct_campaign_outcomes(
    fake_client, monkeypatch,
):
    """THE PIN. Both of these hand back ``campaign is None``, so anything that
    collapses the pair -- reverting to `.single()`, or a caller that reads only
    the campaign -- makes a two-second database fault indistinguishable from a
    permanent verdict on the one action that hands work to a wet lab.

    Written as one test over both outcomes rather than two, because the claim is
    about the DIFFERENCE and a pair of separate assertions can each keep passing
    while the difference disappears.
    """
    absent = read_campaign(str(uuid.uuid4()), user_id="user-1")
    monkeypatch.setattr(cc, "get_service_client", lambda: _RaisingClient())
    unreadable = read_campaign(str(uuid.uuid4()), user_id="user-1")
    assert absent.campaign is None and unreadable.campaign is None
    assert absent.outcome != unreadable.outcome
    assert absent.unavailable is False
    assert unreadable.unavailable is True


def test_read_campaign_applies_the_owner_scope(fake_client):
    """``user_id`` is a QUERY FILTER, so another tenant's campaign matches no row
    and comes back ABSENT -- not OK, and not a distinct "forbidden" outcome,
    which would mean reading a row the scope exists to withhold.

    Dropping ``.eq("user_id", ...)`` from `read_campaign` reds this: the fake
    really filters, so the row would come back and the outcome would be OK.
    """
    camp = _a_campaign(fake_client, user_id="user-1")
    theirs = read_campaign(camp.id, user_id="someone-else")
    assert theirs.outcome == CAMPAIGN_READ_ABSENT
    assert theirs.campaign is None
    # And the unscoped read still works, so the test above failed on the scope
    # rather than on the id.
    assert read_campaign(camp.id).outcome == CAMPAIGN_READ_OK


def test_read_campaign_reports_unavailable_for_a_row_it_cannot_parse(fake_client):
    """The one departure from ``shared.jobs.read_job``'s body, asserted.

    ``_campaign_or_none`` answers None for a row missing a column
    ``ComputeCampaign.from_row`` subscripts. The row is PRESENT, so ABSENT is
    false, and there is no campaign to hand back, so OK is false. Not reachable
    from any row the migrations pin -- this is cover for a partial migration.
    """
    fake_client.store["compute_campaigns"] = [{"id": "c-bad", "user_id": "user-1"}]
    read = read_campaign("c-bad", user_id="user-1")
    assert read.outcome == CAMPAIGN_READ_UNAVAILABLE
    assert read.campaign is None
    # AND `.unavailable` on that same path, which is the property callers
    # actually branch on and which nothing asserted here before. Its docstring
    # used to say "the lookup did not complete", copied verbatim from
    # `JobRead`'s; on THIS path the lookup completed and PostgREST handed back a
    # row. The wording changed and this is what holds the behaviour to it.
    assert read.unavailable is True


class _RaisingTableClient:
    """Fails at ``client.table(...)``, BEFORE any query is built.

    The other raising fake in this file fails at ``execute()``. Both are inside
    `read_campaign`'s ``try`` and both must report UNAVAILABLE, but only one of
    them was exercised: a ``try`` narrowed to the ``execute()`` line alone would
    have left every test here green while a `table()` raise escaped as a 500 out
    of the lab-handoff gate.
    """

    def table(self, _name):
        raise RuntimeError("client is wedged")


def test_read_campaign_reports_unavailable_when_building_the_query_raises(
    monkeypatch,
):
    monkeypatch.setattr(cc, "get_service_client", lambda: _RaisingTableClient())
    read = read_campaign(str(uuid.uuid4()), user_id="user-1")
    assert read.outcome == CAMPAIGN_READ_UNAVAILABLE
    assert read.campaign is None
    assert read.unavailable is True


# ---------------------------------------------------------------------------
# CampaignRead's two guards, and the invariant underneath them
#
# The class docstring claimed "no `__bool__` and no truthiness of any kind"
# while having neither guard: every instance was unconditionally truthy, and
# `frozen=True` GENERATED an `__eq__`, so `read == CAMPAIGN_READ_OK` answered
# False in silence on a read that had succeeded. The precedent for both is
# `tools/proteina/_canary_scoring.py::Verdict`, which paid for the same two
# holes in the same order; `tests/test_proteina_canary.py` pins them there.
# ---------------------------------------------------------------------------


def _ok_read(fake_client):
    return read_campaign(_a_campaign(fake_client).id, user_id="user-1")


def test_a_campaign_read_refuses_to_be_used_as_a_boolean(fake_client, monkeypatch):
    """Asserted on the OK read FIRST, because that is where the default
    behaviour was most dangerous: `if read:` was True there and True on an
    unreadable one, so the natural spelling of "did this work" could not fail.
    """
    for read in (
        _ok_read(fake_client),
        read_campaign(str(uuid.uuid4()), user_id="user-1"),       # absent
        CampaignRead(None, CAMPAIGN_READ_UNAVAILABLE),
    ):
        with pytest.raises(TypeError):
            bool(read)
        with pytest.raises(TypeError):
            if read:            # noqa: SIM103 - the spelling under test
                pass
        with pytest.raises(TypeError):
            not read


def test_a_campaign_read_refuses_to_be_compared_with_an_outcome_string(
    fake_client,
):
    """`__bool__` raising leaves a hole exactly its own size unless `__eq__`
    closes it too: the frozen dataclass's generated `__eq__` returned False
    SILENTLY for `read == CAMPAIGN_READ_OK` on a read that had succeeded, which
    reads as a clean negative rather than as a mistake.

    Every route into `__eq__` is covered, because closing only the direct one
    leaves three spellings of the same error working.
    """
    read = _ok_read(fake_client)
    assert read.outcome == CAMPAIGN_READ_OK
    with pytest.raises(TypeError):
        read == CAMPAIGN_READ_OK
    with pytest.raises(TypeError):
        CAMPAIGN_READ_OK == read            # the reflected comparison
    with pytest.raises(TypeError):
        read != CAMPAIGN_READ_ABSENT        # `!=` routes through `__eq__`
    with pytest.raises(TypeError):
        read in (CAMPAIGN_READ_OK, CAMPAIGN_READ_ABSENT)   # and so does `in`
    # And the cross-family mixup, which is the half of finding 4b a comparison
    # guard CAN catch: all three families spell OK as the string "ok", so this
    # raises for being a string at all rather than for being the wrong one.
    from shared.targets import TARGET_READ_OK
    with pytest.raises(TypeError):
        read == TARGET_READ_OK


def test_two_campaign_reads_still_compare_as_values(fake_client):
    """Refusing the string comparison must not cost ordinary equality, and it
    must not cost hashability either: declaring `__eq__` sets `__hash__` to
    None, which would make a frozen value type unusable in a set.

    THE SECOND READ IS BUILT FROM A SECOND, INDEPENDENTLY CONSTRUCTED CAMPAIGN,
    and that is the whole content of the test rather than a detail of it.
    `__eq__` compares `(self.campaign, self.outcome) == (other.campaign,
    other.outcome)`, and tuple `==` short-circuits on IDENTITY per element --
    so two reads sharing one ComputeCampaign compare equal whether the payload
    comparison works or not, and this test passed unchanged against an `__eq__`
    rewritten to `self.campaign is other.campaign`. Two `read_campaign` calls
    against the same stored row give two distinct objects with equal fields,
    which is the property
    `tests/test_proteina_canary.py::test_two_verdicts_still_compare_as_values`
    gets from building its second Verdict with its own `{"k": 1}`.
    """
    camp = _a_campaign(fake_client)
    first = read_campaign(camp.id, user_id="user-1").campaign
    twin = read_campaign(camp.id, user_id="user-1").campaign
    assert twin is not first, "two reads must build two objects"
    assert twin == first, "and they must be equal to each other by value"
    a = CampaignRead(first, CAMPAIGN_READ_OK)
    assert a == CampaignRead(twin, CAMPAIGN_READ_OK)
    assert a != CampaignRead(None, CAMPAIGN_READ_ABSENT)
    assert CampaignRead(None, CAMPAIGN_READ_ABSENT) != CampaignRead(
        None, CAMPAIGN_READ_UNAVAILABLE)
    # A DIFFERENT campaign, so equality is not merely "same outcome".
    assert a != CampaignRead(_a_campaign(fake_client), CAMPAIGN_READ_OK)
    assert len({a, CampaignRead(twin, CAMPAIGN_READ_OK)}) == 1
    # Not equal to some other type, and not raising either: only strings raise.
    assert a != 17


def test_a_campaign_read_that_is_ok_must_carry_a_campaign(fake_client):
    """The invariant nothing enforced. `CampaignRead(None, CAMPAIGN_READ_OK)`
    constructed fine, and every caller that checks `.outcome` and then reads
    `.campaign` would have been handed a None it has no branch for."""
    with pytest.raises(ValueError):
        CampaignRead(None, CAMPAIGN_READ_OK)


def test_a_campaign_read_that_is_not_ok_must_carry_no_campaign(fake_client):
    """The other direction, which matters just as much: a payload on an
    UNAVAILABLE read is a campaign we are simultaneously claiming not to have
    obtained."""
    camp = _a_campaign(fake_client)
    for outcome in (CAMPAIGN_READ_ABSENT, CAMPAIGN_READ_UNAVAILABLE):
        with pytest.raises(ValueError):
            CampaignRead(camp, outcome)


def test_a_campaign_read_refuses_an_outcome_that_is_not_one_of_the_three():
    """A typo'd outcome is a branch that silently never fires -- `.unavailable`
    answers False and every `== CAMPAIGN_READ_*` test answers False, so the read
    reports as OK-shaped without being OK."""
    with pytest.raises(ValueError):
        CampaignRead(None, "unavailble")


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


def test_a_missing_required_amount_falls_back_to_words(caplog):
    """Unreachable from ``campaign_preauth`` (``required_usd`` defaults to
    ``Decimal("0")``), so this pins the guard, not a live path. Note the
    fallback reads oddly in this particular template -- "does not cover the
    first batch of this campaign (about the first batch to start)" -- which is
    tolerable precisely because nothing in production can produce it.

    The log assertion is what pins the ``is not None`` half. Without it the test
    passes against ``if True``: ``Decimal(str(None))`` raises InvalidOperation,
    the coercion guard catches it, and ``shown`` ends up None either way. The
    difference is that the fallback was CHOSEN rather than recovered from, and
    the warning is the only place that shows.
    """
    pre = SimpleNamespace(
        ok=False, reason=cc.PREAUTH_INSUFFICIENT,
        balance_usd=Decimal("0"), budget_usd=Decimal("50"), required_usd=None,
    )
    with caplog.at_level(logging.WARNING, logger="shared.compute_campaigns"):
        msg = cc.preauth_message(pre)
    assert "(about the first batch to start)" in msg
    assert "$" not in msg
    assert "unrenderable required_usd" not in caplog.text, (
        "None reached the Decimal coercion and was recovered by the except "
        "clause; the `is not None` guard should have skipped it outright"
    )


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


def test_fund_campaign_reports_false_with_no_client(monkeypatch, caplog):
    """The route funds N rows in a loop; a missing client must read as "did not
    start", never as success.

    The log assertion is what makes this test able to fail. Returning False is
    NOT evidence the `client is None` guard exists: delete the guard and
    `None.table(...)` raises AttributeError inside `_cas_transition`'s own bare
    `except Exception`, which returns False too -- byte-identical result, test
    still green. The guard's only observable signature is that it returns
    WITHOUT going through the handler, so the handler's warning must be absent.
    """
    monkeypatch.setattr(cc, "get_service_client", lambda: None)
    with caplog.at_level(logging.WARNING, logger="shared.compute_campaigns"):
        assert cc.fund_campaign("camp-1") is False
    assert "_cas_transition failed" not in caplog.text, (
        "the no-client path went through the exception handler, which means it "
        "reached the client instead of being refused up front"
    )


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


# ---------------------------------------------------------------------------
# 2dp display strings: which direction each one is allowed to lose
# ---------------------------------------------------------------------------
#
# Money is carried at 4dp and shown at 2dp. That conversion used to happen in
# the launch page as `Number(x).toFixed(2)`, which rounds to NEAREST, so a
# $573.6736 hold printed as "$573.67" -- below the amount reserved, above a
# checkbox consenting to "the amount above will be held". These pin the
# direction, which is the entire content of the fix.


@pytest.mark.parametrize("exact,shown", [
    ("573.6736", "573.68"),   # the reported case: nearest would say 573.67
    ("2.0101", "2.02"),        # rfdiffusion@12 budget
    ("2.6219", "2.63"),        # rfdiffusion@12 first wave
    ("0.0001", "0.01"),        # a hold of a hundredth of a cent still holds a cent
    ("30.0000", "30.00"),      # already whole cents: unchanged, not bumped
    ("0", "0.00"),
])
def test_a_displayed_cost_never_falls_below_the_real_one(exact, shown):
    """Red if the rounding flips to nearest, floor, or truncation."""
    assert cc.display_cost_usd(Decimal(exact)) == shown
    assert Decimal(shown) >= Decimal(exact)


@pytest.mark.parametrize("exact,shown", [
    ("573.6736", "573.67"),   # ceiling would claim 573.68 the wallet lacks
    ("2.0199", "2.01"),
    ("0.0099", "0.00"),        # under a cent available is not a cent available
    ("30.0000", "30.00"),
    ("0", "0.00"),
])
def test_a_displayed_balance_never_rises_above_the_real_one(exact, shown):
    """The mirror image, and the reason there are two functions not one."""
    assert cc.display_balance_usd(Decimal(exact)) == shown
    assert Decimal(shown) <= Decimal(exact)


def test_the_two_display_directions_actually_differ():
    """Anti-vacuity: if both helpers rounded the same way, every assertion above
    would still pass for whichever direction happened to be shared. This is the
    one figure that proves they are opposites."""
    assert cc.display_cost_usd(Decimal("573.6736")) == "573.68"
    assert cc.display_balance_usd(Decimal("573.6736")) == "573.67"


@pytest.mark.parametrize("fn", [cc.display_cost_usd, cc.display_balance_usd])
def test_a_display_helper_fails_closed_rather_than_guessing(fn):
    """Unlike ``preauth_message``, these do NOT swallow. Silently rendering
    "0.00" for a broken figure would arm the button against a price nobody
    computed.

    Two claims removed from this docstring rather than repeated. **"Their only
    caller is the estimate endpoint"** is false: both refusal paths and the
    ``display_cost_usd`` Jinja global on ``runs/detail.html`` / ``runs/list.html``
    also call them, and those are plain renders where a raise is a 500 rather
    than a blocked spend. **"Unticking consent"** is false and is contradicted
    twice in ``shared/compute_campaigns.py`` itself, once by a line that says in
    so many words not to describe it that way; the mechanism is the DISABLED
    submit button. See that module's caller enumeration, which is the one place
    this is written down, and which has itself been wrong twice."""
    for bad in ["abc", float("nan"), float("inf"), None, object()]:
        with pytest.raises(Exception):
            fn(bad)


def test_the_total_helper_also_fails_closed():
    """``display_total_usd`` is the third helper and was left out of the
    parametrized case above, so the module docstring's "all three helpers raise"
    was asserted for two of them.

    It takes an iterable rather than a scalar, which is why it does not fit the
    parametrization -- and that is exactly the kind of small awkwardness that
    leaves a helper uncovered while a comment says otherwise. NaN matters most
    here: ``Decimal.quantize`` does NOT signal on NaN, so a non-finite row would
    render "NaN" into a consent panel unless the explicit ``is_finite`` check
    fires first."""
    for bad in [["abc"], [float("nan")], ["1.00", float("inf")], [None]]:
        with pytest.raises(Exception):
            cc.display_total_usd(bad)
    # The empty sum is the one case that must NOT raise.
    assert cc.display_total_usd([]) == "0.00"


# ---------------------------------------------------------------------------
# An unreadable row must not escape as a 500
# ---------------------------------------------------------------------------


def test_an_unreadable_row_reads_as_none_rather_than_raising(caplog):
    """``from_row`` subscripts five columns directly, and four call sites had it
    OUTSIDE the try that makes them total. The fund/drive loop calls
    ``get_campaign`` after the commit point, where a raise would 500 a request
    that has already spent money AND release the idempotency claim with it.

    Red if ``_campaign_or_none`` stops catching, and red if it catches silently.
    """
    with caplog.at_level(logging.WARNING, logger="shared.compute_campaigns"):
        assert cc._campaign_or_none({"id": "c-1"}) is None          # no status
        assert cc._campaign_or_none({}) is None                      # no id
        assert cc._campaign_or_none(None) is None
    assert caplog.text.count("unreadable campaign row") == 3


def test_a_list_of_runs_drops_the_unreadable_row_and_keeps_the_rest(fake_client):
    """A read-only run strip must survive one bad row. The docstring's promise
    that "a short run strip beats a 500" was only true of the paging error; the
    row conversion sat outside it, so one malformed row took the whole page.

    Red if ``list_campaigns_for_target`` converts rows with bare ``from_row``.
    """
    good = _seed_campaign(fake_client, status="funded")
    rows = fake_client.store["compute_campaigns"]
    for row in rows:
        row["target_id"] = "t-1"
    rows.append(_unreadable_row(target_id="t-1"))

    out = cc.list_campaigns_for_target("t-1", user_id="u-1")
    assert [c.id for c in out] == [good]


def _unreadable_row(**extra):
    """A row this query could actually deliver, that ``from_row`` cannot read.

    It carries a real ``status`` on purpose. An earlier version left status out,
    which reads as a stronger malformation but is one PostgREST filters away
    first: ``neq("status", "draft")`` renders ``status <> 'draft'``, which is
    NULL for a NULL status, so the row is dropped server-side and never reaches
    the conversion. ``tool`` and ``preset`` are the missing keys instead, and
    ``from_row`` subscripts both.
    """
    return {
        "id": "bad-row", "user_id": "u-1", "status": "funded", **extra,
    }


def test_a_null_status_row_never_reaches_the_conversion(fake_client):
    """Pins the fake's neq semantics, which decide what the code under test can
    even be handed.

    PostgREST renders ``.neq("status", "draft")`` as ``status <> 'draft'``,
    which is NULL for a NULL status, so the row is DROPPED. Python's ``!=``
    keeps it. This row is otherwise perfectly readable, so if the filter kept it
    it would come back as a campaign and this assertion would fail.

    It is also why the unreadable-row fixtures carry a real status: a NULL
    status is a malformation this query cannot deliver, so a test built on one
    would pin a shape production never produces.
    """
    good = _seed_campaign(fake_client, status="funded")
    rows = fake_client.store["compute_campaigns"]
    for row in rows:
        row["target_id"] = "t-1"
    rows.append({
        "id": "null-status", "user_id": "u-1", "target_id": "t-1",
        "tool": "rfdiffusion", "preset": "pilot", "status": None,
        "requested_designs": 1, "chunk_size": 1, "total_subjobs": 1,
        "budget_usd": "1.0000", "created_at": "2026-07-03T00:00:00Z",
    })

    out = cc.list_campaigns_for_target("t-1", user_id="u-1")
    assert [c.id for c in out] == [good]


def test_one_unreadable_row_does_not_take_the_whole_campaign_list(fake_client):
    """``list_campaigns_for_user`` feeds /campaigns and the homepage.

    Red if it converts rows with bare ``from_row``.
    """
    good = _seed_campaign(fake_client, status="funded")
    fake_client.store["compute_campaigns"].append(_unreadable_row())

    out = cc.list_campaigns_for_user("u-1")
    assert [c.id for c in out] == [good]


def test_an_unreadable_row_makes_get_campaign_return_none_not_raise(fake_client):
    """The one that matters most: the fund/drive loop calls ``get_campaign``
    AFTER the commit point, to confirm what a failed ``fund_campaign`` really
    did. A raise there 500s a request that has already spent money, and
    ``shared/idempotency.py`` releases the claim on any status >= 400, so the
    retry the error invites would fund a second full set.

    Red if ``get_campaign`` converts with bare ``from_row``.
    """
    fake_client.store["compute_campaigns"] = [_unreadable_row()]

    assert cc.get_campaign("bad-row") is None


def test_an_unreadable_row_does_not_abort_the_paused_sweep(fake_client):
    """``sweep_paused_campaigns`` is cron housekeeping over every paused
    campaign. A raise part way leaves the campaigns AFTER the bad row
    unfinalized and un-renotified, silently, until someone reads the cron log.

    So the assertion is that the good row still gets its notification, not
    merely that the call returns: a sweep that aborted on row one would also
    "not raise" if the bad row were last. The bad row is seeded first.

    Red if the sweep converts with bare ``from_row``.
    """
    PAUSED = "paused_insufficient_funds"
    good = _seed_campaign(fake_client, status=PAUSED, campaign_id="camp-good")
    rows = fake_client.store["compute_campaigns"]
    # Recent, so the TTL half of the sweep leaves both rows alone and this test
    # is about the renotify half only.
    for row in rows:
        row["paused_at"] = datetime.now(timezone.utc).isoformat()
    rows.insert(0, _unreadable_row(status=PAUSED))

    with patch.object(cc, "_notify_campaign_paused") as notify:
        summary = cc.sweep_paused_campaigns()

    assert summary == {"finalized": 0, "renotified": 1}
    assert [c.args[0].id for c in notify.call_args_list] == [good]
