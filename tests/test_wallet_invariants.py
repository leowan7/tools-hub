"""Property-style invariant tests for the wallet ledger.

The hypothesis library is not in the project's requirements, so these
tests run a deterministic but randomized walk through the ledger and
assert that the ledger invariants hold at every step. The walk is
seeded so failures reproduce.

Invariants checked (see plan lines 240-251):

1. ``user_wallets.balance_usd == sum(wallet_transactions.amount_usd)``
   under the sign convention applied by the fake RPC layer.
2. Every ``hold_release`` and ``charge`` row references a parent hold.
3. ``stripe_event_id`` is unique across the ``wallet_transactions``
   table.
4. ``auto_reload`` count in last 24 hours is at most 1 per user.
5. No ``charge`` row exceeds the per-tool absolute cap.
6. Credits minus charges minus absorbed variance is greater than or
   equal to zero at every step (no money created from thin air).
7. Concurrent ``reserve_hold`` calls cannot over-allocate the balance.
"""

from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import patch

import pytest

from shared import wallet
from shared.wallet import (
    PER_JOB_HARD_CAP_USD,
    reserve_hold,
    settle_hold,
    top_up_wallet,
)
from tests.test_wallet import _FakeClient, _seed_wallet, _Store  # noqa: PLC0415


USER = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture
def fresh():
    store = _Store()
    client = _FakeClient(store)
    with patch(
        "shared.wallet.get_service_client", return_value=client
    ), patch(
        "shared.credits.get_service_client", return_value=client
    ), patch(
        "shared.wallet_funnel.get_service_client", return_value=client
    ), patch.object(wallet, "_send_email_safe", lambda *a, **kw: None):
        yield store, client


def _signed_total(store: _Store, user_id: str) -> Decimal:
    """Ledger total under the sign convention used by ``_recompute_balance``.

    Absorbed variance does not reduce the user balance; it is recorded
    for Ranomics-side bookkeeping after the hard cap clamps the charge.
    """
    total = Decimal("0")
    for tx in store.tables["wallet_transactions"]:
        if tx["user_id"] != user_id:
            continue
        kind = tx["kind"]
        amount = Decimal(str(tx["amount_usd"]))
        if kind in {"topup", "auto_reload", "signup_credit", "promo",
                    "adjustment", "hold_release"}:
            total += amount
        elif kind in {"charge", "hold"}:
            total -= amount
    return total


def _wallet_balance(store: _Store, user_id: str) -> Decimal:
    row = next(r for r in store.tables["user_wallets"] if r["user_id"] == user_id)
    return Decimal(str(row["balance_usd"]))


def _assert_invariants(store: _Store, user_id: str) -> None:
    # Invariant 1: balance == signed ledger sum
    assert _wallet_balance(store, user_id) == _signed_total(store, user_id), (
        "balance must equal signed ledger total"
    )

    # Invariant 2: every hold_release and charge has a parent_tx_id
    for tx in store.tables["wallet_transactions"]:
        if tx["kind"] in {"hold_release", "charge", "absorbed_variance"}:
            assert tx.get("parent_tx_id") is not None, (
                f"{tx['kind']} row is missing parent_tx_id"
            )

    # Invariant 3: unique stripe_event_id
    event_ids = [
        tx.get("stripe_event_id")
        for tx in store.tables["wallet_transactions"]
        if tx.get("stripe_event_id")
    ]
    assert len(event_ids) == len(set(event_ids)), "stripe_event_id duplicate"

    # Invariant 4: auto_reload count in last 24h <= 1
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_ar = [
        tx for tx in store.tables["wallet_transactions"]
        if tx["user_id"] == user_id
        and tx["kind"] == "auto_reload"
        and tx.get("created_at", "") >= cutoff.isoformat()
    ]
    assert len(recent_ar) <= 1, "auto_reload count in last 24h exceeded 1"

    # Invariant 5: no charge exceeds per-tool cap
    for tx in store.tables["wallet_transactions"]:
        if tx["kind"] != "charge":
            continue
        tool = tx.get("tool_slug")
        if not tool:
            continue
        cap = PER_JOB_HARD_CAP_USD.get(tool)
        if cap is not None:
            assert Decimal(str(tx["amount_usd"])) <= cap, (
                f"charge for {tool} exceeded per-tool cap"
            )

    # Invariant 6: customer credits >= customer charges (no money from
    # thin air). absorbed_variance is excluded because the customer is
    # not debited for it.
    credits = sum(
        Decimal(str(tx["amount_usd"]))
        for tx in store.tables["wallet_transactions"]
        if tx["user_id"] == user_id
        and tx["kind"] in {"topup", "auto_reload", "signup_credit", "promo",
                           "adjustment"}
    )
    debits = sum(
        Decimal(str(tx["amount_usd"]))
        for tx in store.tables["wallet_transactions"]
        if tx["user_id"] == user_id
        and tx["kind"] == "charge"
    )
    assert credits >= debits, "money created from thin air"


# ---------------------------------------------------------------------------
# Sequence walks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_random_sequence_preserves_invariants(seed, fresh):
    """Run 50 random ops against a wallet; check invariants at every step."""
    store, _ = fresh
    rng = random.Random(seed)
    _seed_wallet(store, USER, balance=Decimal("0"))

    actions = ["topup", "hold_and_settle", "hold_and_release",
               "topup_dup_event", "settle_overrun"]
    counter = 0
    for step in range(50):
        action = rng.choice(actions)
        counter += 1
        if action == "topup":
            amount = Decimal(str(rng.choice([20, 50, 100, 200])))
            top_up_wallet(
                USER, amount,
                stripe_payment_intent_id=f"pi_{seed}_{step}",
                stripe_event_id=f"evt_{seed}_{step}",
                kind="topup",
            )
        elif action == "topup_dup_event":
            # Re-emit a previous event id (idempotency exercise).
            if step > 0:
                prev = max(step - 1, 0)
                top_up_wallet(
                    USER, Decimal("99"),
                    stripe_payment_intent_id=f"pi_{seed}_{prev}",
                    stripe_event_id=f"evt_{seed}_{prev}",
                    kind="topup",
                )
        elif action == "hold_and_settle":
            amount = Decimal(str(rng.choice([0.05, 0.10, 0.50, 1.00])))
            hold = reserve_hold(USER, "bindcraft", counter, amount, {})
            if hold is not None:
                settle_hold(hold, gpu_seconds=rng.randint(1, 30),
                            gpu_class="L4", params={})
        elif action == "hold_and_release":
            amount = Decimal(str(rng.choice([0.20, 0.40])))
            hold = reserve_hold(USER, "bindcraft", counter, amount, {})
            if hold is not None:
                wallet.release_hold(hold)
        elif action == "settle_overrun":
            amount = Decimal(str(rng.choice([0.10, 1.00])))
            hold = reserve_hold(USER, "alphafold2", counter, amount, {})
            if hold is not None:
                settle_hold(hold, gpu_seconds=10_000_000,
                            gpu_class="A100-80GB", params={})
        _assert_invariants(store, USER)


def test_concurrent_submits_cannot_over_allocate(fresh):
    """N concurrent reserve_hold workers cannot collectively spend more than balance."""
    store, _ = fresh
    _seed_wallet(store, USER, balance=Decimal("10.00"))

    held: list[Optional[str]] = []
    lock = threading.Lock()

    def worker(i: int):
        # bindcraft base cap $8 leaves room for 0.30 holds.
        hold = reserve_hold(USER, "bindcraft", i, Decimal("0.30"), {})
        with lock:
            held.append(hold)

    with ThreadPoolExecutor(max_workers=50) as ex:
        futures = [ex.submit(worker, i) for i in range(100)]
        for f in futures:
            f.result()

    success = sum(1 for h in held if h is not None)
    # 10 / 0.30 = 33.33 -> 33 successes possible
    assert 1 <= success <= 33
    balance = _wallet_balance(store, USER)
    assert balance >= Decimal("0")
    _assert_invariants(store, USER)


def test_idempotent_settle_does_not_double_charge(fresh):
    store, _ = fresh
    _seed_wallet(store, USER, balance=Decimal("100.00"))
    hold = reserve_hold(USER, "bindcraft", 1, Decimal("0.50"), {})
    settle_hold(hold, gpu_seconds=10, gpu_class="L4", params={})
    settle_hold(hold, gpu_seconds=10, gpu_class="L4", params={})
    charges = [
        tx for tx in store.tables["wallet_transactions"]
        if tx.get("parent_tx_id") == hold and tx["kind"] == "charge"
    ]
    assert len(charges) == 1
    _assert_invariants(store, USER)


def test_idempotent_top_up_does_not_double_credit(fresh):
    store, _ = fresh
    _seed_wallet(store, USER, balance=Decimal("0"))
    top_up_wallet(
        USER, Decimal("50"),
        stripe_payment_intent_id="pi_x", stripe_event_id="evt_x", kind="topup",
    )
    top_up_wallet(
        USER, Decimal("50"),
        stripe_payment_intent_id="pi_x", stripe_event_id="evt_x", kind="topup",
    )
    txs = [
        tx for tx in store.tables["wallet_transactions"]
        if tx.get("stripe_event_id") == "evt_x"
    ]
    assert len(txs) == 1
    _assert_invariants(store, USER)
