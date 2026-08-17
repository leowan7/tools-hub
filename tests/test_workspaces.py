"""Unit tests for the Target Workspace lifecycle.

Uses a fake Supabase client so the tests run offline — no Railway /
Supabase config required. Mirrors the pattern in
``tests/test_idempotency.py``.

Coverage
--------
* SKU configuration + dataclass roundtrip
* First-Workspace refund eligibility (7-day window)
* Second-Workspace refund eligibility (None)
* Stripe PaymentIntent idempotency on activate
* Cap budget tracking and is_within_cap flips
* 80% warning threshold crossing
* Refund flow flips status and clears further submissions
* Expiration cron flips active->expired past TTL
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import patch

import pytest

from billing.tiers import SKU_NAMES, get_sku
from shared.workspaces import (
    DEFAULT_USD_PER_SECOND,
    GPU_USD_PER_SECOND,
    REFUND_WINDOW_DAYS,
    Workspace,
    activate_workspace,
    charge_for_job,
    charge_workspace,
    compute_modal_cost_usd,
    crossed_warn_threshold,
    expire_workspaces,
    get_active_workspace,
    get_workspace,
    list_active_workspaces,
    request_refund,
    sku_config,
    workspace_preflight,
)


# ---------------------------------------------------------------------------
# Fake Supabase client — just enough surface for workspaces queries
# ---------------------------------------------------------------------------


class _FakeTable:
    """In-memory stand-in for a Supabase ``workspaces`` table."""

    def __init__(self, store: list[dict]) -> None:
        self._store = store
        self._filters: list[tuple[str, str, Any]] = []
        self._order: Optional[tuple[str, bool]] = None
        self._limit: Optional[int] = None
        self._select_cols: Optional[str] = None
        self._update_payload: Optional[dict] = None
        self._pending_insert: Optional[dict] = None
        self._return_single: bool = False

    def select(self, cols: str = "*") -> "_FakeTable":
        self._select_cols = cols
        return self

    def eq(self, column: str, value: Any) -> "_FakeTable":
        self._filters.append((column, "=", value))
        return self

    def gt(self, column: str, value: Any) -> "_FakeTable":
        self._filters.append((column, ">", value))
        return self

    def lt(self, column: str, value: Any) -> "_FakeTable":
        self._filters.append((column, "<", value))
        return self

    def order(self, column: str, desc: bool = False) -> "_FakeTable":
        self._order = (column, desc)
        return self

    def limit(self, n: int) -> "_FakeTable":
        self._limit = n
        return self

    def maybe_single(self) -> "_FakeTable":
        self._return_single = True
        return self

    def single(self) -> "_FakeTable":
        self._return_single = True
        return self

    def insert(self, payload: dict) -> "_FakeTable":
        self._pending_insert = payload
        return self

    def update(self, payload: dict) -> "_FakeTable":
        self._update_payload = payload
        return self

    def execute(self) -> Any:
        if self._pending_insert is not None:
            row = dict(self._pending_insert)
            row.setdefault("id", f"ws-{len(self._store) + 1:04d}")
            row.setdefault("created_at", _now_iso())
            self._store.append(row)
            return type("R", (), {"data": [row]})()

        # Apply filters.
        rows = list(self._store)
        for col, op, val in self._filters:
            if op == "=":
                rows = [r for r in rows if r.get(col) == val]
            elif op == ">":
                rows = [r for r in rows if str(r.get(col, "")) > str(val)]
            elif op == "<":
                rows = [r for r in rows if str(r.get(col, "")) < str(val)]

        if self._update_payload is not None:
            for r in rows:
                r.update(self._update_payload)
            return type("R", (), {"data": rows})()

        if self._order:
            col, desc = self._order
            rows.sort(key=lambda r: str(r.get(col, "")), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]

        if self._return_single:
            data = rows[0] if rows else None
            return type("R", (), {"data": data})()
        return type("R", (), {"data": rows})()


class _FakeClient:
    def __init__(self, store: dict[str, list[dict]]) -> None:
        self._store = store

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self._store.setdefault(name, []))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    return {"workspaces": [], "credits_ledger": []}


@pytest.fixture
def fake_client(store):
    return _FakeClient(store)


@pytest.fixture(autouse=True)
def patch_supabase(fake_client):
    """Patch the service client used by both workspaces and credits.

    record_grant / record_spend / etc. all go through get_service_client,
    so a single patch covers writes.
    """
    with patch(
        "shared.workspaces.get_service_client", return_value=fake_client
    ), patch(
        "shared.credits.get_service_client", return_value=fake_client
    ):
        yield


USER_A = "00000000-0000-0000-0000-000000000001"
USER_B = "00000000-0000-0000-0000-000000000002"


# ---------------------------------------------------------------------------
# SKU configuration
# ---------------------------------------------------------------------------


def test_sku_names_are_canonical():
    assert SKU_NAMES == ("workspace_standard", "workspace_xl")


def test_workspace_standard_config():
    sku = get_sku("workspace_standard")
    assert sku is not None
    assert sku.modal_cap_usd == 100.00
    assert sku.duration_days == 30
    assert sku.refund_eligible is True
    assert sku.list_price_usd == 499.00


def test_workspace_xl_config():
    sku = get_sku("workspace_xl")
    assert sku is not None
    assert sku.modal_cap_usd == 500.00
    assert sku.list_price_usd == 2499.00


def test_unknown_sku_returns_none():
    assert get_sku("workspace_pro") is None
    assert sku_config("nonsense") is None


# ---------------------------------------------------------------------------
# Activation + first-Workspace refund eligibility
# ---------------------------------------------------------------------------


def test_activate_first_workspace_grants_7day_refund_window():
    ws = activate_workspace(
        user_id=USER_A,
        target_pdb_id="4Z18",
        sku="workspace_standard",
        target_label="PD-L1 IgV",
        stripe_payment_intent_id="pi_test_1",
        stripe_event_id="evt_test_1",
    )
    assert ws is not None
    assert ws.sku == "workspace_standard"
    assert ws.modal_cap_usd == 100.00
    assert ws.modal_spent_usd == 0.00
    assert ws.status == "active"
    assert ws.refund_eligible_until is not None
    # Window should be ~7 days out.
    delta = ws.refund_eligible_until - ws.activated_at
    assert REFUND_WINDOW_DAYS - 1 <= delta.days <= REFUND_WINDOW_DAYS
    assert ws.refund_eligible_now is True


def test_activate_second_workspace_no_refund_window():
    activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_1",
    )
    second = activate_workspace(
        user_id=USER_A, target_pdb_id="1HEW",
        sku="workspace_standard", stripe_payment_intent_id="pi_2",
    )
    assert second is not None
    assert second.refund_eligible_until is None
    assert second.refund_eligible_now is False


def test_each_user_gets_their_own_refund_window():
    """User B's first Workspace gets a window regardless of user A."""
    activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_A1",
    )
    b_first = activate_workspace(
        user_id=USER_B, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_B1",
    )
    assert b_first is not None
    assert b_first.refund_eligible_until is not None


def test_activate_idempotent_on_payment_intent():
    """Replaying the same checkout event returns the same row."""
    first = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_dup",
    )
    second = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_dup",
    )
    assert first is not None and second is not None
    assert first.id == second.id


def test_activate_rejects_unknown_sku():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_pro",  # not a real SKU
        stripe_payment_intent_id="pi_x",
    )
    assert ws is None


def test_activate_xl_has_500_cap():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_xl", stripe_payment_intent_id="pi_xl",
    )
    assert ws is not None
    assert ws.modal_cap_usd == 500.00


# ---------------------------------------------------------------------------
# Cap tracking
# ---------------------------------------------------------------------------


def test_charge_workspace_increments_spend():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_c1",
    )
    assert charge_workspace(ws.id, 12.34, tool="rfdiffusion", job_id="j1")
    refreshed = get_workspace(ws.id)
    assert refreshed.modal_spent_usd == 12.34
    assert refreshed.remaining_usd == pytest.approx(87.66)
    assert refreshed.is_within_cap is True


def test_charge_workspace_accumulates():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_c2",
    )
    charge_workspace(ws.id, 30.00, tool="rfdiffusion")
    charge_workspace(ws.id, 25.50, tool="bindcraft")
    refreshed = get_workspace(ws.id)
    assert refreshed.modal_spent_usd == pytest.approx(55.50)


def test_is_within_cap_flips_at_100pct():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_cap",
    )
    charge_workspace(ws.id, 100.00, tool="rfdiffusion")
    refreshed = get_workspace(ws.id)
    assert refreshed.is_within_cap is False
    assert refreshed.remaining_usd == 0.0


def test_warn_threshold_crossing_detection():
    assert crossed_warn_threshold(0, 80, 100) is True
    assert crossed_warn_threshold(0, 79.9, 100) is False
    assert crossed_warn_threshold(80, 90, 100) is False  # already past
    assert crossed_warn_threshold(50, 81, 100) is True
    assert crossed_warn_threshold(0, 0, 100) is False
    # Zero-cap edge case (defensive).
    assert crossed_warn_threshold(0, 10, 0) is False


def test_pct_used_is_capped_at_100():
    ws_obj = Workspace(
        id="x", user_id=USER_A, target_pdb_id="t", target_label=None,
        sku="workspace_standard", modal_cap_usd=100.00, modal_spent_usd=150.00,
        activated_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        refund_eligible_until=None, refunded_at=None, status="active",
        stripe_payment_intent_id=None, stripe_refund_id=None,
    )
    assert ws_obj.pct_used == 100.0


# ---------------------------------------------------------------------------
# Active-workspace lookups
# ---------------------------------------------------------------------------


def test_get_active_workspace_returns_matching_target():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_act1",
    )
    found = get_active_workspace(USER_A, "4Z18")
    assert found is not None
    assert found.id == ws.id


def test_get_active_workspace_misses_other_target():
    activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_act2",
    )
    found = get_active_workspace(USER_A, "1HEW")
    assert found is None


def test_list_active_workspaces():
    activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_list1",
    )
    activate_workspace(
        user_id=USER_A, target_pdb_id="1HEW",
        sku="workspace_xl", stripe_payment_intent_id="pi_list2",
    )
    workspaces = list_active_workspaces(USER_A)
    assert len(workspaces) == 2
    skus = {w.sku for w in workspaces}
    assert skus == {"workspace_standard", "workspace_xl"}


# ---------------------------------------------------------------------------
# Refund flow
# ---------------------------------------------------------------------------


def test_request_refund_flips_status(store):
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_ref1",
    )
    updated = request_refund(ws.id, stripe_refund_id="re_test_1")
    assert updated is not None
    assert updated.status == "refunded"
    assert updated.refunded_at is not None
    assert updated.stripe_refund_id == "re_test_1"


def test_refunded_workspace_not_in_active_list():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_ref2",
    )
    request_refund(ws.id, stripe_refund_id="re_test_2")
    assert list_active_workspaces(USER_A) == []


def test_refund_eligible_now_false_after_refund():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_ref3",
    )
    updated = request_refund(ws.id, stripe_refund_id="re_test_3")
    assert updated.refund_eligible_now is False


# ---------------------------------------------------------------------------
# Expiration
# ---------------------------------------------------------------------------


def test_expire_workspaces_flips_past_ttl(store):
    """Manually backdate a workspace's expires_at, then run the cron."""
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_exp1",
    )
    # Backdate in the store directly.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for row in store["workspaces"]:
        if row["id"] == ws.id:
            row["expires_at"] = past

    expired = expire_workspaces()
    assert expired >= 1
    refreshed = get_workspace(ws.id)
    assert refreshed.status == "expired"


def test_expire_workspaces_leaves_active_alone(store):
    """A workspace within its TTL is not flipped."""
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_exp2",
    )
    expire_workspaces()
    refreshed = get_workspace(ws.id)
    assert refreshed.status == "active"


# ---------------------------------------------------------------------------
# Modal cost translation
# ---------------------------------------------------------------------------


def test_compute_modal_cost_a100_80gb():
    """1 hour on A100-80GB ≈ $3.70."""
    assert compute_modal_cost_usd(3600, "A100-80GB") == pytest.approx(3.70, rel=0.01)


def test_compute_modal_cost_zero_seconds():
    assert compute_modal_cost_usd(0, "A100-80GB") == 0.0
    assert compute_modal_cost_usd(-5, "A100-80GB") == 0.0
    assert compute_modal_cost_usd(None, "A100-80GB") == 0.0


def test_compute_modal_cost_unknown_sku_uses_default():
    cost = compute_modal_cost_usd(3600, "M-future-GPU")
    expected = 3600 * DEFAULT_USD_PER_SECOND
    assert cost == pytest.approx(expected)


def test_compute_modal_cost_known_skus_match_rate_card():
    for sku, rate in GPU_USD_PER_SECOND.items():
        assert compute_modal_cost_usd(3600, sku) == pytest.approx(rate * 3600)


# ---------------------------------------------------------------------------
# Pre-flight gate
# ---------------------------------------------------------------------------


def test_workspace_preflight_no_workspace():
    result = workspace_preflight(USER_A, "4Z18")
    assert result.allow is False
    assert result.workspace is None
    assert result.reason == "no_workspace"
    assert "Workspace" in (result.upgrade_message or "")


def test_workspace_preflight_with_active_workspace_allows():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_pre1",
    )
    result = workspace_preflight(USER_A, "4Z18")
    assert result.allow is True
    assert result.workspace is not None
    assert result.workspace.id == ws.id
    assert result.reason == "ok"


def test_workspace_preflight_cap_exceeded_blocks():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_pre2",
    )
    charge_workspace(ws.id, 100.00, tool="rfdiffusion")
    result = workspace_preflight(USER_A, "4Z18")
    assert result.allow is False
    assert result.reason == "cap_exceeded"
    assert "XL" in (result.upgrade_message or "")


def test_workspace_preflight_wrong_target_blocks():
    """A workspace for one target doesn't unlock another target."""
    activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_pre3",
    )
    result = workspace_preflight(USER_A, "1HEW")
    assert result.allow is False
    assert result.reason == "no_workspace"


# ---------------------------------------------------------------------------
# charge_for_job (post-completion accounting)
# ---------------------------------------------------------------------------


def test_charge_for_job_deducts_from_workspace():
    ws = activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_charge1",
    )
    updated = charge_for_job(
        USER_A, "4Z18",
        gpu_seconds=600, gpu_sku="A100-80GB",
        tool="rfdiffusion", job_id="job-1",
    )
    assert updated is not None
    # 600s * 0.001028 = 0.6168
    assert updated.modal_spent_usd == pytest.approx(0.6168, rel=0.01)


def test_charge_for_job_zero_seconds_is_noop():
    activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_charge2",
    )
    result = charge_for_job(
        USER_A, "4Z18",
        gpu_seconds=0, gpu_sku="A100-80GB",
        tool="rfdiffusion", job_id="job-2",
    )
    assert result is None


def test_charge_for_job_no_workspace_returns_none():
    result = charge_for_job(
        USER_A, "nonexistent",
        gpu_seconds=600, gpu_sku="A100-80GB",
        tool="rfdiffusion", job_id="job-3",
    )
    assert result is None


def test_charge_for_job_accumulates_across_jobs():
    activate_workspace(
        user_id=USER_A, target_pdb_id="4Z18",
        sku="workspace_standard", stripe_payment_intent_id="pi_charge4",
    )
    charge_for_job(
        USER_A, "4Z18",
        gpu_seconds=1800, gpu_sku="A100-40GB",
        tool="rfdiffusion", job_id="job-a",
    )
    charge_for_job(
        USER_A, "4Z18",
        gpu_seconds=600, gpu_sku="A100-80GB",
        tool="bindcraft", job_id="job-b",
    )
    final = get_active_workspace(USER_A, "4Z18")
    assert final is not None
    # 1800 * 0.000714 + 600 * 0.001028 = 1.2852 + 0.6168 = 1.902
    assert final.modal_spent_usd == pytest.approx(1.902, rel=0.01)
