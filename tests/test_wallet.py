"""Unit tests for :mod:`shared.wallet`.

The tests use an in-memory fake Supabase client so the suite runs
offline. The fake covers the tables and RPCs the wallet module exercises:

* ``user_wallets``      (selects, inserts, updates)
* ``wallet_transactions`` (selects + filters + inserts via RPCs)
* ``funnel_alerts``     (insert, select)
* RPCs: ``credit_wallet``, ``try_hold_for_job``, ``settle_hold``,
  ``release_hold``

Each RPC stub mirrors the integrity guarantees from the spec
(idempotency on stripe_event_id, row-locked balance check on holds,
hold-amount clamping in settlement).

Concurrency is exercised via a thread pool against a re-entrant lock
on the fake client, mirroring the row-lock guarantee of the real
SQL function.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from unittest.mock import patch

import pytest

from shared import wallet
from shared.wallet import (
    DEFAULT_DAILY_CAP_USD,
    DEFAULT_AUTO_RELOAD_MONTHLY_CAP_USD,
    GPU_USD_PER_SECOND,
    LOW_BALANCE_EMAIL_THRESHOLD,
    PER_JOB_HARD_CAP_USD,
    REASON_INSUFFICIENT,
    REASON_OK,
    REASON_PER_TOOL_CAP,
    REASON_SELF_SERVE_CEILING,
    REASON_WALLET_FROZEN,
    SIGNUP_CREDIT_USD,
    SELF_SERVE_CEILING_USD,
    WALLET_MARKUP,
    auto_reload_if_needed,
    compute_charge_usd,
    compute_modal_cost_usd,
    freeze_wallet_on_dispute,
    get_or_create_wallet,
    gpu_usd_per_second,
    record_signup_credit,
    release_hold,
    requires_wallet,
    reserve_hold,
    settle_hold,
    top_up_wallet,
    wallet_preflight,
)


USER_A = "00000000-0000-0000-0000-000000000001"
USER_B = "00000000-0000-0000-0000-000000000002"


# ---------------------------------------------------------------------------
# Fake Supabase client + RPC layer
# ---------------------------------------------------------------------------


class _Store:
    """Shared mutable state for the fake client.

    Holds tables plus a re-entrant lock that serializes RPC mutations
    to mirror the row-lock guarantee on ``user_wallets``.
    """

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {
            "user_wallets": [],
            "wallet_transactions": [],
            "funnel_alerts": [],
        }
        self.lock = threading.RLock()
        # next_id used so insert results are deterministic and sortable
        self.next_id = 1

    def fresh_id(self) -> str:
        with self.lock:
            n = self.next_id
            self.next_id += 1
            return f"tx-{n:06d}"


class _Table:
    """In-memory implementation of the subset of the Supabase API we use."""

    def __init__(self, store: _Store, name: str) -> None:
        self._store = store
        self._name = name
        self._rows = store.tables.setdefault(name, [])
        self._filters: list[tuple[str, str, Any]] = []
        self._order: Optional[tuple[str, bool]] = None
        self._limit: Optional[int] = None
        self._select: Optional[str] = None
        self._single: bool = False
        self._pending_update: Optional[dict] = None
        self._pending_insert: Optional[dict] = None

    def select(self, cols: str = "*") -> "_Table":
        self._select = cols
        return self

    def eq(self, col: str, val: Any) -> "_Table":
        self._filters.append((col, "=", val))
        return self

    def in_(self, col: str, vals) -> "_Table":
        self._filters.append((col, "in", list(vals)))
        return self

    def gte(self, col: str, val: Any) -> "_Table":
        self._filters.append((col, ">=", val))
        return self

    def gt(self, col: str, val: Any) -> "_Table":
        self._filters.append((col, ">", val))
        return self

    def lt(self, col: str, val: Any) -> "_Table":
        self._filters.append((col, "<", val))
        return self

    def order(self, col: str, desc: bool = False) -> "_Table":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_Table":
        self._limit = n
        return self

    def maybe_single(self) -> "_Table":
        self._single = True
        return self

    def single(self) -> "_Table":
        self._single = True
        return self

    def insert(self, payload: dict) -> "_Table":
        self._pending_insert = payload
        return self

    def update(self, payload: dict) -> "_Table":
        self._pending_update = payload
        return self

    def execute(self) -> Any:
        with self._store.lock:
            if self._pending_insert is not None:
                row = dict(self._pending_insert)
                row.setdefault("id", self._store.fresh_id())
                row.setdefault(
                    "created_at", datetime.now(timezone.utc).isoformat()
                )
                self._rows.append(row)
                return type("R", (), {"data": [dict(row)]})()

            rows = list(self._rows)
            for col, op, val in self._filters:
                if op == "=":
                    rows = [r for r in rows if r.get(col) == val]
                elif op == "in":
                    rows = [r for r in rows if r.get(col) in val]
                elif op == ">=":
                    rows = [r for r in rows if str(r.get(col, "")) >= str(val)]
                elif op == ">":
                    rows = [r for r in rows if str(r.get(col, "")) > str(val)]
                elif op == "<":
                    rows = [r for r in rows if str(r.get(col, "")) < str(val)]

            if self._pending_update is not None:
                touched = []
                for r in rows:
                    r.update(self._pending_update)
                    touched.append(dict(r))
                return type("R", (), {"data": touched})()

            if self._order:
                col, desc = self._order
                rows.sort(key=lambda r: str(r.get(col, "")), reverse=desc)
            if self._limit is not None:
                rows = rows[: self._limit]
            if self._single:
                return type("R", (), {"data": dict(rows[0]) if rows else None})()
            return type("R", (), {"data": [dict(r) for r in rows]})()


class _Rpc:
    """Implements the SQL helper functions the wallet code calls."""

    def __init__(self, store: _Store, name: str, params: dict) -> None:
        self._store = store
        self._name = name
        self._params = dict(params or {})

    def execute(self) -> Any:
        with self._store.lock:
            method = getattr(self, f"_rpc_{self._name}", None)
            if method is None:
                raise AssertionError(f"unhandled RPC: {self._name}")
            return method()

    # --- helpers -----------------------------------------------------------

    def _wallet_row(self, user_id: str) -> dict:
        for r in self._store.tables["user_wallets"]:
            if r["user_id"] == user_id:
                return r
        raise AssertionError(f"wallet row missing for {user_id}")

    def _recompute_balance(self, user_id: str) -> Decimal:
        total = Decimal("0")
        for tx in self._store.tables["wallet_transactions"]:
            if tx["user_id"] != user_id:
                continue
            kind = tx["kind"]
            amount = Decimal(str(tx["amount_usd"]))
            if kind in {"topup", "auto_reload", "signup_credit", "promo",
                        "adjustment", "hold_release"}:
                total += amount
            elif kind in {"charge", "hold"}:
                total -= amount
            # absorbed_variance is Ranomics-side bookkeeping. It does not
            # reduce the user wallet (the user already paid up to the hard
            # cap via the charge row).
        return total

    # --- RPCs --------------------------------------------------------------

    def _rpc_credit_wallet(self) -> Any:
        p = self._params
        user_id = p["p_user_id"]
        amount = Decimal(str(p["p_amount_usd"]))
        kind = p["p_kind"]
        stripe_event_id = p.get("p_stripe_event_id")
        # Idempotency
        if stripe_event_id:
            for tx in self._store.tables["wallet_transactions"]:
                if tx.get("stripe_event_id") == stripe_event_id:
                    return type("R", (), {"data": tx["id"]})()
        tx_id = self._store.fresh_id()
        self._store.tables["wallet_transactions"].append({
            "id": tx_id,
            "user_id": user_id,
            "kind": kind,
            "amount_usd": float(amount),
            "stripe_event_id": stripe_event_id,
            "stripe_payment_intent_id": p.get("p_stripe_payment_intent_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        # Bump wallet.balance_usd
        wallet = self._wallet_row(user_id)
        wallet["balance_usd"] = float(self._recompute_balance(user_id))
        return type("R", (), {"data": tx_id})()

    def _rpc_try_hold_for_job(self) -> Any:
        p = self._params
        user_id = p["p_user_id"]
        amount = Decimal(str(p["p_amount_usd"]))
        wallet = self._wallet_row(user_id)
        if wallet.get("wallet_frozen"):
            return type("R", (), {"data": None})()
        # Recompute balance under lock
        balance = self._recompute_balance(user_id)
        if balance < amount:
            return type("R", (), {"data": None})()
        # Hard cap clamp (mirrors SQL guard)
        hard_cap = Decimal(str(p.get("p_hard_cap_usd") or 0))
        if hard_cap > 0 and amount > hard_cap:
            return type("R", (), {"data": None})()
        tx_id = self._store.fresh_id()
        self._store.tables["wallet_transactions"].append({
            "id": tx_id,
            "user_id": user_id,
            "kind": "hold",
            "amount_usd": float(amount),
            "estimated_cost_usd": float(amount),
            "tool_slug": p.get("p_tool_slug"),
            "job_id": p.get("p_job_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "settled": False,
        })
        wallet["balance_usd"] = float(self._recompute_balance(user_id))
        return type("R", (), {"data": tx_id})()

    def _rpc_settle_hold(self) -> Any:
        p = self._params
        hold_id = p["p_hold_tx_id"]
        actual = Decimal(str(p["p_actual_usd"]))
        hard_cap = Decimal(str(p["p_hard_cap_usd"]))
        # Idempotency
        for tx in self._store.tables["wallet_transactions"]:
            if tx.get("parent_tx_id") == hold_id and tx["kind"] == "charge":
                return type("R", (), {"data": False})()
        hold = next(
            (
                tx
                for tx in self._store.tables["wallet_transactions"]
                if tx["id"] == hold_id
            ),
            None,
        )
        if hold is None or hold.get("settled"):
            return type("R", (), {"data": False})()
        held_amount = Decimal(str(hold["amount_usd"]))
        capped_actual = min(actual, hard_cap)
        # Reverse the hold
        self._store.tables["wallet_transactions"].append({
            "id": self._store.fresh_id(),
            "user_id": hold["user_id"],
            "kind": "hold_release",
            "amount_usd": float(held_amount),
            "parent_tx_id": hold_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        # Charge actual (clamped)
        self._store.tables["wallet_transactions"].append({
            "id": self._store.fresh_id(),
            "user_id": hold["user_id"],
            "kind": "charge",
            "amount_usd": float(capped_actual),
            "parent_tx_id": hold_id,
            "tool_slug": hold.get("tool_slug"),
            "job_id": hold.get("job_id"),
            "gpu_seconds": p.get("p_gpu_seconds"),
            "gpu_class": p.get("p_gpu_class"),
            "failure_reason": p.get("p_failure_reason"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        # Absorbed variance: anything above hard cap is eaten
        if actual > hard_cap:
            absorbed = actual - hard_cap
            self._store.tables["wallet_transactions"].append({
                "id": self._store.fresh_id(),
                "user_id": hold["user_id"],
                "kind": "absorbed_variance",
                "amount_usd": float(absorbed),
                "parent_tx_id": hold_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        hold["settled"] = True
        wallet = self._wallet_row(hold["user_id"])
        wallet["balance_usd"] = float(self._recompute_balance(hold["user_id"]))
        return type("R", (), {"data": True})()

    def _rpc_release_hold(self) -> Any:
        p = self._params
        hold_id = p["p_hold_tx_id"]
        hold = next(
            (
                tx
                for tx in self._store.tables["wallet_transactions"]
                if tx["id"] == hold_id
            ),
            None,
        )
        if hold is None or hold.get("settled"):
            return type("R", (), {"data": False})()
        self._store.tables["wallet_transactions"].append({
            "id": self._store.fresh_id(),
            "user_id": hold["user_id"],
            "kind": "hold_release",
            "amount_usd": float(hold["amount_usd"]),
            "parent_tx_id": hold_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        hold["settled"] = True
        wallet = self._wallet_row(hold["user_id"])
        wallet["balance_usd"] = float(self._recompute_balance(hold["user_id"]))
        return type("R", (), {"data": True})()


class _FakeClient:
    def __init__(self, store: Optional[_Store] = None) -> None:
        self.store = store or _Store()

    def table(self, name: str) -> _Table:
        return _Table(self.store, name)

    def rpc(self, name: str, params: dict):
        return _Rpc(self.store, name, params)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> _Store:
    return _Store()


@pytest.fixture
def fake_client(store) -> _FakeClient:
    return _FakeClient(store)


@pytest.fixture(autouse=True)
def patch_clients(fake_client):
    """Patch every code path that looks up the service client.

    Each module imports ``get_service_client`` from
    ``shared.credits``; patching at each call site is the safe way to
    make sure no real Supabase connection is attempted.
    """
    with patch(
        "shared.wallet.get_service_client", return_value=fake_client
    ), patch(
        "shared.credits.get_service_client", return_value=fake_client
    ), patch(
        "shared.wallet_funnel.get_service_client", return_value=fake_client
    ):
        yield fake_client


@pytest.fixture
def email_log():
    """Capture wallet email dispatches."""
    sent: list[tuple[str, dict]] = []

    real_send = wallet._send_email_safe

    def fake(func_name: str, **kwargs):
        sent.append((func_name, dict(kwargs)))
        return None

    with patch.object(wallet, "_send_email_safe", side_effect=fake):
        yield sent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_wallet(
    store: _Store,
    user_id: str,
    *,
    balance: Decimal = Decimal("100.00"),
    frozen: bool = False,
    auto_reload_enabled: bool = False,
    auto_reload_threshold: Decimal = Decimal("20.00"),
    auto_reload_amount: Decimal = Decimal("50.00"),
    auto_reload_monthly_cap: Decimal = DEFAULT_AUTO_RELOAD_MONTHLY_CAP_USD,
    payment_method: Optional[str] = "pm_test",
    customer: Optional[str] = "cus_test",
    daily_cap: Decimal = DEFAULT_DAILY_CAP_USD,
) -> None:
    store.tables["user_wallets"].append({
        "user_id": user_id,
        "balance_usd": float(balance),
        "wallet_frozen": frozen,
        "wallet_frozen_reason": None,
        "auto_reload_enabled": auto_reload_enabled,
        "auto_reload_threshold_usd": float(auto_reload_threshold),
        "auto_reload_amount_usd": float(auto_reload_amount),
        "auto_reload_monthly_cap_usd": float(auto_reload_monthly_cap),
        "stripe_payment_method_id": payment_method,
        "stripe_customer_id": customer,
        "daily_spend_cap_usd": float(daily_cap),
    })
    # Seed the credit transaction so balance can be recomputed.
    if balance > 0:
        store.tables["wallet_transactions"].append({
            "id": store.fresh_id(),
            "user_id": user_id,
            "kind": "topup",
            "amount_usd": float(balance),
            "stripe_event_id": f"seed:{user_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })


# ===========================================================================
# Tests
# ===========================================================================


# --- Modal cost helpers ----------------------------------------------------


def test_gpu_usd_per_second_known_class():
    assert gpu_usd_per_second("A100-80GB") == GPU_USD_PER_SECOND["A100-80GB"]


def test_gpu_usd_per_second_unknown_class_returns_default():
    assert gpu_usd_per_second(None) == wallet.DEFAULT_USD_PER_SECOND
    assert gpu_usd_per_second("not-a-gpu") == wallet.DEFAULT_USD_PER_SECOND


def test_compute_modal_cost_usd_zero_seconds():
    assert compute_modal_cost_usd(0, "A100-80GB") == Decimal("0")
    assert compute_modal_cost_usd(-5, "A100-80GB") == Decimal("0")


def test_compute_modal_cost_usd_value():
    cost = compute_modal_cost_usd(60, "A100-80GB")
    expected = Decimal("60") * Decimal(str(GPU_USD_PER_SECOND["A100-80GB"]))
    assert cost.quantize(Decimal("0.000001")) == expected.quantize(
        Decimal("0.000001")
    )


def test_compute_charge_usd_applies_markup():
    raw = compute_modal_cost_usd(60, "A100-80GB")
    charge = compute_charge_usd(60, "A100-80GB")
    assert charge.quantize(Decimal("0.0001")) == (raw * WALLET_MARKUP).quantize(
        Decimal("0.0001")
    )


# --- Wallet bootstrap + signup credit --------------------------------------


def test_get_or_create_wallet_creates_with_signup_credit(store, fake_client, email_log):
    wallet_row = get_or_create_wallet(USER_A)
    assert wallet_row is not None
    assert wallet_row["user_id"] == USER_A
    # Signup credit landed
    txs = [
        tx for tx in store.tables["wallet_transactions"]
        if tx["user_id"] == USER_A and tx["kind"] == "signup_credit"
    ]
    assert len(txs) == 1
    assert Decimal(str(txs[0]["amount_usd"])) == SIGNUP_CREDIT_USD
    # Balance reflects the credit
    assert Decimal(str(wallet_row["balance_usd"])) == SIGNUP_CREDIT_USD


def test_get_or_create_wallet_idempotent(store, fake_client):
    first = get_or_create_wallet(USER_A)
    second = get_or_create_wallet(USER_A)
    assert first is not None and second is not None
    # No duplicate wallet row
    assert len([r for r in store.tables["user_wallets"] if r["user_id"] == USER_A]) == 1
    # No duplicate signup credit
    credits = [
        tx for tx in store.tables["wallet_transactions"]
        if tx["user_id"] == USER_A and tx["kind"] == "signup_credit"
    ]
    assert len(credits) == 1


def test_record_signup_credit_idempotent(store):
    record_signup_credit(USER_A)
    record_signup_credit(USER_A)
    credits = [
        tx for tx in store.tables["wallet_transactions"]
        if tx["user_id"] == USER_A and tx["kind"] == "signup_credit"
    ]
    assert len(credits) == 1


# --- Top-ups ---------------------------------------------------------------


def test_top_up_wallet_credits_balance(store):
    _seed_wallet(store, USER_A, balance=Decimal("0"))
    top_up_wallet(
        USER_A,
        Decimal("50.00"),
        stripe_payment_intent_id="pi_1",
        stripe_event_id="evt_1",
        kind="topup",
    )
    wallet_row = next(r for r in store.tables["user_wallets"] if r["user_id"] == USER_A)
    assert Decimal(str(wallet_row["balance_usd"])) == Decimal("50.00")


def test_top_up_wallet_idempotent_on_stripe_event_id(store):
    _seed_wallet(store, USER_A, balance=Decimal("0"))
    top_up_wallet(
        USER_A, Decimal("50.00"),
        stripe_payment_intent_id="pi_1", stripe_event_id="evt_1", kind="topup",
    )
    top_up_wallet(
        USER_A, Decimal("50.00"),
        stripe_payment_intent_id="pi_1", stripe_event_id="evt_1", kind="topup",
    )
    txs = [
        tx for tx in store.tables["wallet_transactions"]
        if tx.get("stripe_event_id") == "evt_1"
    ]
    assert len(txs) == 1


def test_top_up_wallet_rejects_non_positive_amount():
    with pytest.raises(ValueError):
        top_up_wallet(
            USER_A, Decimal("0"),
            stripe_payment_intent_id="pi", stripe_event_id="evt", kind="topup",
        )
    with pytest.raises(ValueError):
        top_up_wallet(
            USER_A, Decimal("-5"),
            stripe_payment_intent_id="pi", stripe_event_id="evt", kind="topup",
        )


def test_top_up_wallet_rejects_unknown_kind():
    with pytest.raises(ValueError):
        top_up_wallet(
            USER_A, Decimal("5"),
            stripe_payment_intent_id="pi", stripe_event_id="evt", kind="freebie",
        )


def test_top_up_wallet_auto_reload_kind_allowed(store):
    _seed_wallet(store, USER_A, balance=Decimal("0"))
    top_up_wallet(
        USER_A, Decimal("25.00"),
        stripe_payment_intent_id="pi_ar", stripe_event_id="evt_ar",
        kind="auto_reload",
    )
    txs = [
        tx for tx in store.tables["wallet_transactions"]
        if tx.get("stripe_event_id") == "evt_ar"
    ]
    assert len(txs) == 1
    assert txs[0]["kind"] == "auto_reload"


# --- Preflight -------------------------------------------------------------


def test_preflight_allows_when_balance_sufficient(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    pre = wallet_preflight(USER_A, "mpnn", Decimal("0.05"), {})
    assert pre.allow is True
    assert pre.reason == REASON_OK
    assert pre.deficit_usd == Decimal("0")


def test_preflight_blocks_when_balance_insufficient(store):
    _seed_wallet(store, USER_A, balance=Decimal("0.10"))
    pre = wallet_preflight(USER_A, "bindcraft", Decimal("4.40"), {})
    assert pre.allow is False
    assert pre.reason == REASON_INSUFFICIENT
    assert pre.deficit_usd > 0


def test_preflight_blocks_when_frozen(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"), frozen=True)
    pre = wallet_preflight(USER_A, "mpnn", Decimal("0.05"), {})
    assert pre.allow is False
    assert pre.reason == REASON_WALLET_FROZEN


def test_preflight_blocks_when_self_serve_ceiling_exceeded(store):
    _seed_wallet(store, USER_A, balance=Decimal("100000.00"))
    pre = wallet_preflight(
        USER_A,
        "rfantibody",
        SELF_SERVE_CEILING_USD + Decimal("1"),
        {"num_designs": 200},
    )
    assert pre.allow is False
    assert pre.reason == REASON_SELF_SERVE_CEILING


def test_preflight_blocks_when_per_tool_cap_exceeded(store):
    _seed_wallet(store, USER_A, balance=Decimal("100000.00"))
    pre = wallet_preflight(
        USER_A,
        "alphafold2",
        Decimal("999"),  # alphafold2 base cap is 1.50, no scaling param
        {},
    )
    assert pre.allow is False
    assert pre.reason == REASON_PER_TOOL_CAP


def test_preflight_no_longer_blocks_on_daily_spend(store):
    """Phase 2 fund-and-drain retired the per-day spend cap.

    A job that would have tripped the old $10 daily cap (a prior $9.50 hold
    today plus a new $2.00 job) now passes as long as the prepaid balance
    covers it. The prepaid balance is the only spend ceiling.
    """
    _seed_wallet(
        store, USER_A,
        balance=Decimal("1000.00"),
        daily_cap=Decimal("10.00"),
    )
    # Seed a prior hold today (a job submitted earlier today).
    store.tables["wallet_transactions"].append({
        "id": store.fresh_id(),
        "user_id": USER_A,
        "kind": "hold",
        "amount_usd": -9.50,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # Use bindcraft so the per-tool cap does not trip.
    pre = wallet_preflight(USER_A, "bindcraft", Decimal("2.00"), {})
    assert pre.allow is True
    assert pre.reason == REASON_OK


def test_spent_today_nets_holds_releases_and_charges(store):
    """_spent_today_usd nets each job's settlement against its hold.

    Spend = |holds| - |releases| + |charges|. A job that settles under
    estimate has its surplus netted out; an overrun adds a charge. This is
    the figure shown as "Spent today" on the wallet overview. The per-day
    spend cap that once consumed it was retired in Phase 2 fund-and-drain,
    so this now just verifies the netting math the display relies on.
    """
    _seed_wallet(store, USER_A, balance=Decimal("1000.00"))
    now = datetime.now(timezone.utc).isoformat()

    def _add(kind, amount):
        store.tables["wallet_transactions"].append({
            "id": store.fresh_id(),
            "user_id": USER_A,
            "kind": kind,
            "amount_usd": amount,
            "created_at": now,
        })

    # Job 1 held $5, settled at $3: hold -5, release +2 -> net $3.
    _add("hold", -5.00)
    _add("hold_release", 2.00)
    # Job 2 held $3, overran to $4: hold -3, charge -1 -> net $4.
    _add("hold", -3.00)
    _add("charge", -1.00)

    # Net spend today = |holds|(8) - |releases|(2) + |charges|(1) = $7.
    assert wallet._spent_today_usd(USER_A) == Decimal("7.00")


def test_net_spend_usd_nets_and_excludes_non_job_rows(store):
    """_net_spend_usd nets holds/releases/charges, ignores other kinds."""
    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
    from shared.wallet import _net_spend_usd  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat()
    for kind, amount in [
        ("hold", -5.00), ("hold_release", 2.00), ("charge", -1.50),
        ("hold", -3.00), ("absorbed_variance", -9.00),
        ("topup", 100.00), ("signup_credit", 5.00),
    ]:
        store.tables["wallet_transactions"].append({
            "id": store.fresh_id(),
            "user_id": USER_A,
            "kind": kind,
            "amount_usd": amount,
            "created_at": now,
        })
    since = _dt(2026, 1, 1, tzinfo=_tz.utc)
    # holds |5|+|3|=8, releases |2|=2, charges |1.5|=1.5 -> 8 - 2 + 1.5.
    # absorbed_variance, topup, signup_credit are excluded.
    assert _net_spend_usd(USER_A, since) == Decimal("7.5")


# --- reserve_hold ----------------------------------------------------------


def test_reserve_hold_success_returns_id(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    hold_id = reserve_hold(USER_A, "mpnn", 42, Decimal("0.10"), {})
    assert hold_id is not None
    holds = [
        tx for tx in store.tables["wallet_transactions"]
        if tx.get("kind") == "hold" and tx["user_id"] == USER_A
    ]
    assert len(holds) == 1
    assert Decimal(str(holds[0]["amount_usd"])) == Decimal("0.10")


def test_reserve_hold_returns_none_when_insufficient(store):
    _seed_wallet(store, USER_A, balance=Decimal("0.05"))
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("1.00"), {})
    assert hold_id is None


def test_reserve_hold_returns_none_when_frozen(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"), frozen=True)
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("1.00"), {})
    assert hold_id is None


def test_reserve_hold_rejects_non_positive_amount():
    with pytest.raises(ValueError):
        reserve_hold(USER_A, "bindcraft", 1, Decimal("0"), {})


def test_reserve_hold_reduces_recomputed_balance(store):
    _seed_wallet(store, USER_A, balance=Decimal("10.00"))
    # bindcraft base cap is $8 so a $3 hold fits.
    reserve_hold(USER_A, "bindcraft", 1, Decimal("3.00"), {})
    wallet_row = next(
        r for r in store.tables["user_wallets"] if r["user_id"] == USER_A
    )
    assert Decimal(str(wallet_row["balance_usd"])) == Decimal("7.00")


def test_two_holds_consume_balance(store):
    _seed_wallet(store, USER_A, balance=Decimal("10.00"))
    # Use bindcraft because base cap of $8 fits $4-$5 holds.
    a = reserve_hold(USER_A, "bindcraft", 1, Decimal("4.00"), {})
    b = reserve_hold(USER_A, "bindcraft", 2, Decimal("5.00"), {})
    c = reserve_hold(USER_A, "bindcraft", 3, Decimal("5.00"), {})
    assert a is not None and b is not None
    assert c is None, "third hold must fail: balance was 1.00 remaining"


# --- settle_hold ----------------------------------------------------------


def test_settle_hold_releases_surplus_on_below_estimate(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    # Use bindcraft so a $0.50 hold sits well below the $8 base cap.
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("0.50"), {})
    assert hold_id is not None
    # Actual is 0.10
    settle_hold(hold_id, gpu_seconds=10, gpu_class="L4", params={})
    wallet_row = next(
        r for r in store.tables["user_wallets"] if r["user_id"] == USER_A
    )
    balance = Decimal(str(wallet_row["balance_usd"]))
    # Original $100 minus actual charge for 10s on L4 (times markup)
    expected_actual = (Decimal("10") * Decimal(str(GPU_USD_PER_SECOND["L4"]))
                       * WALLET_MARKUP).quantize(Decimal("0.0001"))
    assert balance > Decimal("99")
    assert balance == (Decimal("100.00") - expected_actual).quantize(
        Decimal("0.0001")
    )


def test_settle_hold_clamps_actual_to_hard_cap(store):
    _seed_wallet(store, USER_A, balance=Decimal("1000.00"))
    spec_cap = PER_JOB_HARD_CAP_USD["alphafold2"]
    hold_id = reserve_hold(USER_A, "alphafold2", 1, Decimal("1.00"), {})
    assert hold_id is not None
    # Simulate runaway: 10000s on A100-80GB -> way above hard cap.
    settle_hold(hold_id, gpu_seconds=10_000_000, gpu_class="A100-80GB", params={})
    charges = [
        tx for tx in store.tables["wallet_transactions"] if tx["kind"] == "charge"
    ]
    assert len(charges) == 1
    assert Decimal(str(charges[0]["amount_usd"])) <= spec_cap


def test_settle_hold_records_absorbed_variance_on_overrun(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    hold_id = reserve_hold(USER_A, "alphafold2", 1, Decimal("1.00"), {})
    settle_hold(hold_id, gpu_seconds=10_000_000, gpu_class="A100-80GB", params={})
    absorbed = [
        tx for tx in store.tables["wallet_transactions"]
        if tx["kind"] == "absorbed_variance"
    ]
    assert len(absorbed) == 1


def test_settle_hold_idempotent_on_replay(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("0.50"), {})
    settle_hold(hold_id, gpu_seconds=10, gpu_class="L4", params={})
    settle_hold(hold_id, gpu_seconds=10, gpu_class="L4", params={})
    charges = [
        tx for tx in store.tables["wallet_transactions"]
        if tx.get("parent_tx_id") == hold_id and tx["kind"] == "charge"
    ]
    assert len(charges) == 1


def test_settle_hold_records_failure_reason(store):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("0.50"), {})
    settle_hold(
        hold_id, gpu_seconds=3, gpu_class="L4", params={},
        failure_reason="overrun_safety_kill",
    )
    charge = next(
        tx for tx in store.tables["wallet_transactions"] if tx["kind"] == "charge"
    )
    assert charge["failure_reason"] == "overrun_safety_kill"


def test_settle_hold_charges_for_partial_compute_on_failure(store):
    """Even a failure consumes some GPU time and that consumption is charged."""
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("0.50"), {})
    settle_hold(
        hold_id, gpu_seconds=5, gpu_class="L4", params={},
        failure_reason="modal_oom",
    )
    charges = [
        tx for tx in store.tables["wallet_transactions"] if tx["kind"] == "charge"
    ]
    assert len(charges) == 1
    assert Decimal(str(charges[0]["amount_usd"])) > 0


def test_settle_hold_unknown_hold_returns_none(store):
    result = settle_hold("missing-id", gpu_seconds=1, gpu_class="L4", params={})
    assert result is None


# --- release_hold ---------------------------------------------------------


def test_release_hold_returns_balance_to_user(store):
    _seed_wallet(store, USER_A, balance=Decimal("10.00"))
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("3.00"), {})
    release_hold(hold_id, reason="cancelled_before_run")
    wallet_row = next(
        r for r in store.tables["user_wallets"] if r["user_id"] == USER_A
    )
    assert Decimal(str(wallet_row["balance_usd"])) == Decimal("10.00")


def test_release_hold_idempotent(store):
    _seed_wallet(store, USER_A, balance=Decimal("10.00"))
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("3.00"), {})
    release_hold(hold_id)
    release_hold(hold_id)
    releases = [
        tx for tx in store.tables["wallet_transactions"]
        if tx.get("parent_tx_id") == hold_id and tx["kind"] == "hold_release"
    ]
    assert len(releases) == 1


# --- Auto-reload ----------------------------------------------------------


def test_auto_reload_not_enabled_returns_not_enabled(store):
    _seed_wallet(store, USER_A, balance=Decimal("10.00"), auto_reload_enabled=False)
    assert auto_reload_if_needed(USER_A) == "not_enabled"


def test_auto_reload_above_threshold_skips(store):
    _seed_wallet(
        store, USER_A,
        balance=Decimal("100.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
    )
    assert auto_reload_if_needed(USER_A) == "above_threshold"


def test_auto_reload_no_payment_method_disables(store, email_log):
    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
        payment_method=None,
    )
    assert auto_reload_if_needed(USER_A) == "no_payment_method"
    wallet_row = next(r for r in store.tables["user_wallets"] if r["user_id"] == USER_A)
    assert wallet_row["auto_reload_enabled"] is False
    assert any(name == "send_auto_reload_failed_email" for name, _ in email_log)


def test_auto_reload_rate_limited_when_already_reloaded_in_24h(store, email_log):
    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
    )
    store.tables["wallet_transactions"].append({
        "id": store.fresh_id(),
        "user_id": USER_A,
        "kind": "auto_reload",
        "amount_usd": 50.0,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    })
    assert auto_reload_if_needed(USER_A) == "rate_limited"
    assert any(
        name == "send_auto_reload_rate_limited_email" for name, _ in email_log
    )


def test_auto_reload_monthly_cap_blocks(store, email_log):
    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
        auto_reload_amount=Decimal("100.00"),
        auto_reload_monthly_cap=Decimal("150.00"),
    )
    # Already $100 reloaded this month.
    #
    # This test was DATE DEPENDENT and went red on 2026-08-01 with no code
    # change, when a long session crossed midnight UTC. Two separate causes,
    # and fixing only the first is not enough:
    #
    # 1. The row was seeded at "now minus 2 days" while the cap sums the
    #    CALENDAR month, so on the 1st and 2nd it landed in the PREVIOUS month,
    #    month_total read 0, the cap did not block, and the call fell through
    #    to a real off-session Stripe charge. Anchored to the month start now.
    # 2. auto_reload_if_needed checks the 24 HOUR rate limit BEFORE the monthly
    #    cap (shared/wallet.py:766 vs :769). Early on the 1st, "inside this
    #    calendar month" and "more than 24 hours ago" have no overlap at all,
    #    so no seed timestamp can reach the cap check. The rate limiter is a
    #    different gate with its own test above, so it is stubbed out here
    #    rather than worked around: this test is about the cap.
    _now = datetime.now(timezone.utc)
    _month_start = _now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    store.tables["wallet_transactions"].append({
        "id": store.fresh_id(),
        "user_id": USER_A,
        "kind": "auto_reload",
        "amount_usd": 100.0,
        "created_at": max(_month_start, _now - timedelta(days=2)).isoformat(),
    })
    with patch("shared.wallet._auto_reload_count_24h", return_value=0):
        assert auto_reload_if_needed(USER_A) == "monthly_cap"
    assert any(
        name == "send_auto_reload_monthly_cap_email" for name, _ in email_log
    )


def test_auto_reload_triggers_when_eligible(store):
    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
        auto_reload_amount=Decimal("50.00"),
        auto_reload_monthly_cap=Decimal("1000.00"),
    )
    # The off-session charge succeeds; the webhook (not this call) credits
    # the wallet, so "triggered" just means the PaymentIntent went out.
    with patch(
        "billing.checkout.create_off_session_payment_intent",
        return_value={"id": "pi_fake"},
    ):
        result = auto_reload_if_needed(USER_A)
    assert result == "triggered"


def test_auto_reload_amount_zero_disables(store, email_log):
    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
        auto_reload_amount=Decimal("0"),
    )
    result = auto_reload_if_needed(USER_A)
    assert result == "no_amount_configured"


def test_auto_reload_no_customer_disables(store, email_log):
    """A saved card with no Stripe customer cannot be charged off-session;
    auto-reload is disabled instead of failing on every settle."""
    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
        customer=None,
    )
    assert auto_reload_if_needed(USER_A) == "no_payment_method"
    wallet_row = next(
        r for r in store.tables["user_wallets"] if r["user_id"] == USER_A
    )
    assert wallet_row["auto_reload_enabled"] is False


def test_auto_reload_disables_on_permanent_stripe_failure(store, email_log):
    """A declined or unusable card disables auto-reload and emails the user."""
    from billing.checkout import OffSessionChargeError  # noqa: PLC0415

    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
        auto_reload_amount=Decimal("50.00"),
    )

    def _boom(**_kwargs):
        raise OffSessionChargeError(
            "declined", retryable=False, reason="expired_card"
        )

    with patch(
        "billing.checkout.create_off_session_payment_intent",
        side_effect=_boom,
    ):
        assert auto_reload_if_needed(USER_A) == "stripe_error"
    wallet_row = next(
        r for r in store.tables["user_wallets"] if r["user_id"] == USER_A
    )
    assert wallet_row["auto_reload_enabled"] is False
    failed = [
        kw for name, kw in email_log
        if name == "send_auto_reload_failed_email"
    ]
    assert failed and failed[0].get("reason") == "expired_card"


def test_auto_reload_stays_enabled_on_retryable_stripe_failure(store, email_log):
    """A transient Stripe outage leaves auto-reload on for the next settle."""
    from billing.checkout import OffSessionChargeError  # noqa: PLC0415

    _seed_wallet(
        store, USER_A,
        balance=Decimal("5.00"),
        auto_reload_enabled=True,
        auto_reload_threshold=Decimal("20.00"),
        auto_reload_amount=Decimal("50.00"),
    )

    def _boom(**_kwargs):
        raise OffSessionChargeError(
            "stripe down", retryable=True, reason="card_declined"
        )

    with patch(
        "billing.checkout.create_off_session_payment_intent",
        side_effect=_boom,
    ):
        assert auto_reload_if_needed(USER_A) == "stripe_error"
    wallet_row = next(
        r for r in store.tables["user_wallets"] if r["user_id"] == USER_A
    )
    assert wallet_row["auto_reload_enabled"] is True
    assert not any(
        name == "send_auto_reload_failed_email" for name, _ in email_log
    )


def test_classify_off_session_error_retryable_vs_permanent():
    """billing.checkout._classify_off_session_error sorts Stripe errors."""
    from billing.checkout import _classify_off_session_error  # noqa: PLC0415

    class CardError(Exception):
        code = "expired_card"

    class InvalidRequestError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    retryable, reason = _classify_off_session_error(CardError())
    assert retryable is False and reason == "expired_card"
    retryable, reason = _classify_off_session_error(InvalidRequestError())
    assert retryable is False and reason == "no_payment_method"
    retryable, _ = _classify_off_session_error(APIConnectionError())
    assert retryable is True


# --- Freeze ---------------------------------------------------------------


def test_freeze_wallet_on_dispute_sets_flag(store, email_log):
    _seed_wallet(store, USER_A, balance=Decimal("100.00"))
    freeze_wallet_on_dispute(USER_A, dispute_id="dp_test")
    wallet_row = next(r for r in store.tables["user_wallets"] if r["user_id"] == USER_A)
    assert wallet_row["wallet_frozen"] is True
    assert wallet_row["wallet_frozen_reason"] == "chargeback_dispute:dp_test"
    assert any(name == "send_wallet_frozen_email" for name, _ in email_log)
    assert any(name == "alert_ops_slack" for name, _ in email_log)


# --- Decorator (definition only) ------------------------------------------


def test_requires_wallet_decorator_is_callable():
    """The decorator must be a callable that returns a callable.

    We do not apply it to any route in this Wave, but we exercise the
    factory so refactors do not silently break the signature.
    """
    deco = requires_wallet("mpnn")
    assert callable(deco)

    @deco
    def handler():
        return "ok"

    assert callable(handler)


# --- Concurrency ----------------------------------------------------------


def test_concurrent_reserves_serialize_through_lock(store):
    """N concurrent reserve_hold calls must not over-debit balance."""
    _seed_wallet(store, USER_A, balance=Decimal("10.00"))

    # Run 10 threads each trying to grab $2. Only 5 should succeed.
    # bindcraft base cap is $8 so a $2 hold is allowed.
    hold_ids: list[Optional[str]] = []
    lock = threading.Lock()

    def worker(i: int):
        h = reserve_hold(USER_A, "bindcraft", i, Decimal("2.00"), {})
        with lock:
            hold_ids.append(h)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(worker, i) for i in range(10)]
        for f in as_completed(futures):
            f.result()

    successes = [h for h in hold_ids if h is not None]
    failures = [h for h in hold_ids if h is None]
    assert len(successes) == 5
    assert len(failures) == 5

    # Balance is now 0
    wallet_row = next(r for r in store.tables["user_wallets"] if r["user_id"] == USER_A)
    assert Decimal(str(wallet_row["balance_usd"])) == Decimal("0.00")


def test_concurrent_two_users_isolated(store):
    """Holds against different wallets do not affect each other."""
    _seed_wallet(store, USER_A, balance=Decimal("5.00"))
    _seed_wallet(store, USER_B, balance=Decimal("5.00"))

    def make_holds(user, amount, n):
        ids = []
        for i in range(n):
            h = reserve_hold(user, "bindcraft", i, Decimal(str(amount)), {})
            ids.append(h)
        return ids

    with ThreadPoolExecutor(max_workers=4) as ex:
        fa = ex.submit(make_holds, USER_A, 1.50, 5)
        fb = ex.submit(make_holds, USER_B, 1.50, 5)
        a_ids = fa.result()
        b_ids = fb.result()

    assert sum(1 for h in a_ids if h is not None) == 3
    assert sum(1 for h in b_ids if h is not None) == 3


# --- Low balance email ----------------------------------------------------


def test_settle_emits_low_balance_email_when_below_threshold(store, email_log):
    _seed_wallet(store, USER_A, balance=Decimal("6.00"))
    hold_id = reserve_hold(USER_A, "bindcraft", 1, Decimal("4.00"), {})
    settle_hold(hold_id, gpu_seconds=100, gpu_class="A100-80GB", params={})
    wallet_row = next(r for r in store.tables["user_wallets"] if r["user_id"] == USER_A)
    if Decimal(str(wallet_row["balance_usd"])) < LOW_BALANCE_EMAIL_THRESHOLD:
        assert any(
            name == "send_low_balance_email" for name, _ in email_log
        )


# --- Preflight email side-effects ----------------------------------------


def test_preflight_email_fires_on_cap_block(store, email_log):
    _seed_wallet(store, USER_A, balance=Decimal("100000.00"))
    pre = wallet_preflight(USER_A, "alphafold2", Decimal("999"), {})
    assert pre.allow is False
    # The preflight check itself does not emit; the reserve_hold call does.
    reserve_hold(USER_A, "alphafold2", 1, Decimal("999"), {})
    assert any(name == "send_job_capped_email" for name, _ in email_log)


def test_round_up_topup_amount_boundaries():
    """_round_up_topup_amount: ceil to nearest $5, floored at MIN_TOPUP_USD ($20).

    Guards the Commit-0 relocation of this helper from app.py into shared.wallet.
    """
    from shared.wallet import MIN_TOPUP_USD, _round_up_topup_amount

    assert MIN_TOPUP_USD == Decimal("20.00")
    # deficit <= 0 short-circuits straight to the floor
    assert _round_up_topup_amount(Decimal("0")) == Decimal("20.00")
    assert _round_up_topup_amount(Decimal("-5")) == Decimal("20.00")
    # small deficits round up to $5 then get floored to the $20 minimum
    assert _round_up_topup_amount(Decimal("0.01")) == Decimal("20.00")
    assert _round_up_topup_amount(Decimal("5.00")) == Decimal("20.00")
    assert _round_up_topup_amount(Decimal("20.00")) == Decimal("20.00")
    # above the floor: ceil to the next $5
    assert _round_up_topup_amount(Decimal("20.01")) == Decimal("25.00")
    assert _round_up_topup_amount(Decimal("22.00")) == Decimal("25.00")
    assert _round_up_topup_amount(Decimal("25.00")) == Decimal("25.00")

