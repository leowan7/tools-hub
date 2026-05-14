"""Unit tests for :mod:`webhooks.stripe`.

The Wave 2 wallet pivot rewrote the webhook handler to subscribe to
exactly four Stripe events:

* ``checkout.session.completed``    user started top up
* ``payment_intent.succeeded``      off session auto reload landed
* ``payment_intent.payment_failed`` off session auto reload declined
* ``charge.dispute.created``        chargeback received

The tests cover signature verification (good and bad), per-event
routing, idempotency on replay, and the contract that:

* ``checkout.session.completed`` credits the wallet with the right
  amount and user id and dispatches ``send_topup_confirmation_email``.
* ``payment_intent.succeeded`` (kind=auto_reload) credits the wallet
  and dispatches ``send_auto_reload_charged_email``.
* ``payment_intent.payment_failed`` (kind=auto_reload) disables
  auto reload and dispatches ``send_auto_reload_failed_email``.
* ``charge.dispute.created`` freezes the wallet.

All Supabase + Stripe interactions are faked so the suite runs offline.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import patch

import pytest
from flask import Flask

from webhooks import stripe as stripe_webhook


USER_A = "00000000-0000-0000-0000-000000000aaa"


# ---------------------------------------------------------------------------
# Fake Supabase client (mirrors the shape used by shared/wallet tests).
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {
            "stripe_events": [],
            "user_wallets": [],
            "wallet_transactions": [],
        }


class _Table:
    def __init__(self, store: _Store, name: str) -> None:
        self._store = store
        self._name = name
        self._rows = store.tables.setdefault(name, [])
        self._filters: list[tuple[str, str, Any]] = []
        self._pending_insert: Optional[dict] = None
        self._pending_update: Optional[dict] = None

    def select(self, *_args: Any, **_kwargs: Any) -> "_Table":
        return self

    def eq(self, col: str, val: Any) -> "_Table":
        self._filters.append((col, "=", val))
        return self

    def insert(self, payload: dict) -> "_Table":
        self._pending_insert = payload
        return self

    def update(self, payload: dict) -> "_Table":
        self._pending_update = payload
        return self

    def execute(self) -> Any:
        if self._pending_insert is not None:
            # Mirror the unique constraint on event_id.
            if self._name == "stripe_events":
                event_id = self._pending_insert.get("event_id")
                for existing in self._rows:
                    if existing.get("event_id") == event_id:
                        raise RuntimeError("duplicate key value")
            self._rows.append(dict(self._pending_insert))
            return type("R", (), {"data": [dict(self._pending_insert)]})()
        rows = list(self._rows)
        for col, op, val in self._filters:
            if op == "=":
                rows = [r for r in rows if r.get(col) == val]
        if self._pending_update is not None:
            touched = []
            for r in rows:
                r.update(self._pending_update)
                touched.append(dict(r))
            return type("R", (), {"data": touched})()
        return type("R", (), {"data": [dict(r) for r in rows]})()


class _FakeClient:
    def __init__(self, store: Optional[_Store] = None) -> None:
        self.store = store or _Store()

    def table(self, name: str) -> _Table:
        return _Table(self.store, name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> _Store:
    return _Store()


@pytest.fixture
def fake_client(store: _Store) -> _FakeClient:
    return _FakeClient(store)


@pytest.fixture
def email_log():
    """Capture every ``_send_email_safe`` call from the webhook module."""
    sent: list[tuple[str, dict]] = []

    def fake(func_name: str, **kwargs: Any) -> None:
        sent.append((func_name, dict(kwargs)))

    with patch.object(stripe_webhook, "_send_email_safe", side_effect=fake):
        yield sent


@pytest.fixture
def wallet_calls():
    """Capture credit/freeze calls so we can assert routing and arguments."""
    captured: dict[str, list[dict]] = {
        "top_up_wallet": [],
        "freeze_wallet_on_dispute": [],
    }

    def fake_top_up(**kwargs: Any) -> dict:
        captured["top_up_wallet"].append(dict(kwargs))
        # Return a populated wallet dict so the handler is happy.
        return {
            "user_id": kwargs.get("user_id"),
            "balance_usd": 100.00,
        }

    def fake_freeze(**kwargs: Any) -> bool:
        captured["freeze_wallet_on_dispute"].append(dict(kwargs))
        return True

    with patch.object(
        stripe_webhook, "top_up_wallet", side_effect=fake_top_up
    ), patch.object(
        stripe_webhook,
        "freeze_wallet_on_dispute",
        side_effect=fake_freeze,
    ):
        yield captured


@pytest.fixture
def patch_clients(fake_client: _FakeClient):
    """Patch the service client lookup everywhere we touch."""
    with patch.object(
        stripe_webhook, "get_service_client", return_value=fake_client
    ):
        yield fake_client


@pytest.fixture
def flask_app(patch_clients) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    stripe_webhook.register_stripe_webhook(app)
    return app


@pytest.fixture
def client(flask_app: Flask):
    return flask_app.test_client()


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------


def _checkout_session_event(
    *,
    event_id: str = "evt_topup_1",
    user_id: str = USER_A,
    amount_usd: str = "20.00",
    payment_intent: str = "pi_topup_1",
    save_pm: Optional[str] = None,
    payment_status: str = "paid",
    kind: str = "topup",
) -> dict:
    metadata: dict[str, str] = {
        "user_id": user_id,
        "amount_usd": amount_usd,
        "kind": kind,
    }
    if save_pm is not None:
        metadata["save_pm"] = save_pm
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "payment_status": payment_status,
                "amount_total": int(float(amount_usd) * 100),
                "currency": "usd",
                "payment_intent": payment_intent,
                "customer": "cus_test",
                "metadata": metadata,
            }
        },
    }


def _payment_intent_succeeded_event(
    *,
    event_id: str = "evt_auto_reload_1",
    user_id: str = USER_A,
    amount_cents: int = 5000,
    pi_id: str = "pi_ar_1",
    kind: str = "auto_reload",
) -> dict:
    return {
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": pi_id,
                "amount": amount_cents,
                "currency": "usd",
                "metadata": {"user_id": user_id, "kind": kind},
            }
        },
    }


def _payment_intent_failed_event(
    *,
    event_id: str = "evt_pi_failed_1",
    user_id: str = USER_A,
    pi_id: str = "pi_ar_2",
    error_message: str = "Your card was declined.",
    kind: str = "auto_reload",
) -> dict:
    return {
        "id": event_id,
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": pi_id,
                "metadata": {"user_id": user_id, "kind": kind},
                "last_payment_error": {"message": error_message},
            }
        },
    }


def _dispute_event(
    *,
    event_id: str = "evt_dispute_1",
    dispute_id: str = "dp_test_1",
    charge_id: str = "ch_test_1",
) -> dict:
    return {
        "id": event_id,
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": dispute_id,
                "charge": charge_id,
            }
        },
    }


def _post_event(client, event: dict, *, signature: str = "ok"):
    return client.post(
        "/webhooks/stripe",
        data=json.dumps(event),
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_bad_signature_returns_400(client):
    """Signature failures must reject with 400 (not 200)."""
    with patch.object(stripe_webhook, "_verify_signature", return_value=None):
        evt = _checkout_session_event()
        resp = _post_event(client, evt, signature="bogus")
    assert resp.status_code == 400
    assert b"invalid signature" in resp.data


def test_malformed_event_id_returns_400(client):
    """Even with a valid signature, an event lacking id or type is rejected."""
    bad_event = {"id": "", "type": ""}
    with patch.object(stripe_webhook, "_verify_signature", return_value=bad_event):
        resp = _post_event(client, bad_event)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Routing: every event reaches the right handler
# ---------------------------------------------------------------------------


def test_checkout_session_routes_to_topup_handler(
    client, wallet_calls, email_log
):
    evt = _checkout_session_event(event_id="evt_route_topup")
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["event_type"] == "checkout.session.completed"
    # top_up_wallet called once with kind=topup
    assert len(wallet_calls["top_up_wallet"]) == 1
    call = wallet_calls["top_up_wallet"][0]
    assert call["kind"] == "topup"
    # Email dispatched
    names = [n for n, _ in email_log]
    assert "send_topup_confirmation_email" in names


def test_payment_intent_succeeded_routes_to_auto_reload_handler(
    client, wallet_calls, email_log
):
    evt = _payment_intent_succeeded_event(event_id="evt_route_ar_success")
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["event_type"] == "payment_intent.succeeded"
    assert len(wallet_calls["top_up_wallet"]) == 1
    assert wallet_calls["top_up_wallet"][0]["kind"] == "auto_reload"
    names = [n for n, _ in email_log]
    assert "send_auto_reload_charged_email" in names


def test_payment_intent_failed_routes_to_disable_auto_reload(
    client, store, email_log, wallet_calls
):
    """Failed auto reload PI flips auto_reload_enabled to false."""
    # Seed a wallet so the update touches a real row.
    store.tables["user_wallets"].append({
        "user_id": USER_A,
        "balance_usd": 5.00,
        "auto_reload_enabled": True,
    })
    evt = _payment_intent_failed_event(event_id="evt_route_ar_failed")
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["event_type"] == "payment_intent.payment_failed"
    # Wallet row mutated.
    wallet_row = next(
        r for r in store.tables["user_wallets"] if r["user_id"] == USER_A
    )
    assert wallet_row["auto_reload_enabled"] is False
    # Email dispatched.
    names = [n for n, _ in email_log]
    assert "send_auto_reload_failed_email" in names


def test_dispute_routes_to_freeze_handler(client, wallet_calls, email_log):
    """charge.dispute.created freezes the wallet."""
    evt = _dispute_event(event_id="evt_route_dispute")
    # Stub the stripe.Charge.retrieve call inside the dispute handler.
    fake_charge = {
        "id": "ch_test_1",
        "metadata": {"user_id": USER_A, "kind": "topup"},
        "payment_intent": "pi_test_1",
    }
    import stripe

    with patch.object(
        stripe_webhook, "_verify_signature", return_value=evt
    ), patch.object(stripe.Charge, "retrieve", return_value=fake_charge):
        resp = _post_event(client, evt)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["event_type"] == "charge.dispute.created"
    assert len(wallet_calls["freeze_wallet_on_dispute"]) == 1
    call = wallet_calls["freeze_wallet_on_dispute"][0]
    assert call["user_id"] == USER_A
    assert call["dispute_id"] == "dp_test_1"


def test_unknown_event_type_is_ignored_with_200(client):
    """An event Stripe should not send still returns 200 with status=ignored.

    Returning 200 stops Stripe from retrying noise; the dashboard config
    is the canonical filter.
    """
    bogus = {
        "id": "evt_bogus_1",
        "type": "customer.subscription.created",
        "data": {"object": {}},
    }
    with patch.object(stripe_webhook, "_verify_signature", return_value=bogus):
        resp = _post_event(client, bogus)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ignored"


# ---------------------------------------------------------------------------
# Checkout amount + user id contract
# ---------------------------------------------------------------------------


def test_checkout_session_uses_metadata_amount_when_present(
    client, wallet_calls
):
    """The amount_usd metadata wins over amount_total in cents."""
    evt = _checkout_session_event(
        event_id="evt_amount_meta",
        amount_usd="50.00",
    )
    # amount_total still in cents in the object (50 dollars = 5000)
    evt["data"]["object"]["amount_total"] = 5000
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        _post_event(client, evt)
    call = wallet_calls["top_up_wallet"][0]
    assert str(call["amount_usd"]) == "50.00"
    assert call["user_id"] == USER_A
    assert call["stripe_event_id"] == "evt_amount_meta"
    assert call["kind"] == "topup"


def test_checkout_session_falls_back_to_amount_total_when_metadata_missing(
    client, wallet_calls
):
    """If metadata.amount_usd is absent, derive from amount_total (cents)."""
    evt = _checkout_session_event(
        event_id="evt_amount_fallback",
        amount_usd="25.00",
    )
    # Strip the metadata amount_usd entry so the fallback fires.
    del evt["data"]["object"]["metadata"]["amount_usd"]
    evt["data"]["object"]["amount_total"] = 2500
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        _post_event(client, evt)
    call = wallet_calls["top_up_wallet"][0]
    assert str(call["amount_usd"]) == "25.00"


def test_checkout_session_skips_when_user_id_missing(
    client, wallet_calls
):
    """Missing user_id metadata means we cannot credit; skip without crashing."""
    evt = _checkout_session_event(event_id="evt_no_user")
    del evt["data"]["object"]["metadata"]["user_id"]
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "skipped"
    assert body["reason"] == "missing_user_id"
    assert wallet_calls["top_up_wallet"] == []


def test_checkout_session_ignores_non_topup_kind(client, wallet_calls):
    """Workspace SKU events are archived; ignore them quietly."""
    evt = _checkout_session_event(event_id="evt_ws_stragler", kind="workspace_xl")
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ignored"
    assert wallet_calls["top_up_wallet"] == []


# ---------------------------------------------------------------------------
# Auto reload kind gating
# ---------------------------------------------------------------------------


def test_payment_intent_succeeded_ignores_non_auto_reload_kind(
    client, wallet_calls
):
    """A manual topup PI still fires payment_intent.succeeded; ignore here.

    Credit responsibility is single sourced through checkout.session.completed.
    """
    evt = _payment_intent_succeeded_event(
        event_id="evt_pi_manual",
        kind="topup",
    )
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    body = resp.get_json()
    assert body["status"] == "ignored"
    assert wallet_calls["top_up_wallet"] == []


def test_payment_intent_failed_ignores_non_auto_reload_kind(
    client, store
):
    """payment_intent.payment_failed for a manual top up PI is ignored."""
    evt = _payment_intent_failed_event(
        event_id="evt_pi_manual_failed",
        kind="topup",
    )
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    body = resp.get_json()
    assert body["status"] == "ignored"


def test_payment_intent_failed_records_reason_from_last_payment_error(
    client, store, email_log
):
    """The failure email carries the Stripe message verbatim."""
    store.tables["user_wallets"].append({
        "user_id": USER_A,
        "balance_usd": 5.0,
        "auto_reload_enabled": True,
    })
    evt = _payment_intent_failed_event(
        event_id="evt_ar_decline",
        error_message="Insufficient funds.",
    )
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        _post_event(client, evt)
    failed_calls = [
        kwargs for name, kwargs in email_log
        if name == "send_auto_reload_failed_email"
    ]
    assert len(failed_calls) == 1
    assert failed_calls[0]["reason"] == "Insufficient funds."


# ---------------------------------------------------------------------------
# Idempotency: replay must be a noop
# ---------------------------------------------------------------------------


def test_replay_is_noop_does_not_double_credit(
    client, wallet_calls
):
    """The second delivery of the same event id must return 'already_processed'.

    The contract: stripe_events.event_id is a primary key. The handler
    catches the duplicate key error and stops the route before any
    wallet primitive is called.
    """
    evt = _checkout_session_event(event_id="evt_replay_1")
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        first = _post_event(client, evt)
        second = _post_event(client, evt)
    assert first.status_code == 200
    assert first.get_json()["status"] == "ok"
    assert second.status_code == 200
    assert second.get_json()["status"] == "already_processed"
    # The credit only fired once.
    assert len(wallet_calls["top_up_wallet"]) == 1


def test_replay_across_event_types_is_isolated(
    client, wallet_calls, store
):
    """Different event types with different ids each insert one row and process once."""
    evts = [
        _checkout_session_event(event_id="evt_isolate_1"),
        _payment_intent_succeeded_event(event_id="evt_isolate_2"),
    ]
    for evt in evts:
        with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
            _post_event(client, evt)
    # Two rows in stripe_events
    assert len(store.tables["stripe_events"]) == 2
    # Two distinct top_up_wallet calls
    assert len(wallet_calls["top_up_wallet"]) == 2


# ---------------------------------------------------------------------------
# Dispute path: charge metadata + PI fallback
# ---------------------------------------------------------------------------


def test_dispute_resolves_user_via_payment_intent_when_charge_metadata_bare(
    client, wallet_calls
):
    """If the charge has no metadata, retrieve the PI for user_id."""
    evt = _dispute_event(event_id="evt_dispute_pi_fallback")
    fake_charge_no_metadata = {
        "id": "ch_test_1",
        "metadata": {},
        "payment_intent": "pi_test_1",
    }
    fake_pi = {
        "id": "pi_test_1",
        "metadata": {"user_id": USER_A, "kind": "topup"},
    }
    import stripe

    with patch.object(
        stripe_webhook, "_verify_signature", return_value=evt
    ), patch.object(
        stripe.Charge, "retrieve", return_value=fake_charge_no_metadata
    ), patch.object(stripe.PaymentIntent, "retrieve", return_value=fake_pi):
        resp = _post_event(client, evt)
    assert resp.status_code == 200
    assert wallet_calls["freeze_wallet_on_dispute"][0]["user_id"] == USER_A


def test_dispute_skips_when_user_unresolvable(client, wallet_calls):
    """A dispute on a charge with no user metadata is logged + skipped."""
    evt = _dispute_event(event_id="evt_dispute_no_user")
    fake_charge = {
        "id": "ch_test_1",
        "metadata": {},
        "payment_intent": None,
    }
    import stripe

    with patch.object(
        stripe_webhook, "_verify_signature", return_value=evt
    ), patch.object(stripe.Charge, "retrieve", return_value=fake_charge):
        resp = _post_event(client, evt)
    body = resp.get_json()
    assert body["status"] == "skipped"
    assert wallet_calls["freeze_wallet_on_dispute"] == []


# ---------------------------------------------------------------------------
# Skipped payment status path
# ---------------------------------------------------------------------------


def test_checkout_session_skips_unpaid_status(client, wallet_calls):
    """A Checkout session with payment_status='unpaid' must not credit."""
    evt = _checkout_session_event(
        event_id="evt_unpaid", payment_status="unpaid"
    )
    with patch.object(stripe_webhook, "_verify_signature", return_value=evt):
        resp = _post_event(client, evt)
    body = resp.get_json()
    assert body["status"] == "skipped"
    assert wallet_calls["top_up_wallet"] == []


# ---------------------------------------------------------------------------
# Helper coverage (amount and metadata extraction)
# ---------------------------------------------------------------------------


def test_amount_from_minor_handles_str_and_int():
    from decimal import Decimal as D
    assert stripe_webhook._amount_from_minor(2000) == D("20.00")
    assert stripe_webhook._amount_from_minor("2500") == D("25.00")
    assert stripe_webhook._amount_from_minor(None) == D("0")
    assert stripe_webhook._amount_from_minor("not a number") == D("0")


def test_user_id_from_metadata_handles_missing():
    assert stripe_webhook._user_id_from_metadata({}) is None
    assert stripe_webhook._user_id_from_metadata({"metadata": {}}) is None
    assert stripe_webhook._user_id_from_metadata(
        {"metadata": {"user_id": "  user_x  "}}
    ) == "user_x"
