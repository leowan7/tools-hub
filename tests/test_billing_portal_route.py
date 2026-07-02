"""`/billing/portal` must resolve the wallet's Stripe customer id (cso L3).

The route previously called ``create_portal_session(return_url=...)``
without the required keyword-only ``customer_id``, raising a TypeError
-> hard 500 on every click. It must now pass the id and degrade to a
friendly ``?portal_error=1`` redirect when the wallet has no saved card.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx(user_id="u-1"):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=100, email="u@example.com"
    )


def test_billing_portal_passes_customer_id(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("app.load_user_context", return_value=_ctx()), patch(
        "app.get_or_create_wallet",
        return_value={"stripe_customer_id": "cus_123"},
    ), patch(
        "billing.checkout.create_portal_session",
        return_value=("https://billing.stripe.com/p/session_abc", None),
    ) as portal:
        resp = client.get("/billing/portal")
    assert resp.status_code == 303
    assert resp.headers["Location"].startswith("https://billing.stripe.com/")
    # The customer id must actually be forwarded, not omitted.
    assert portal.call_args.kwargs["customer_id"] == "cus_123"


def test_billing_portal_no_customer_degrades_gracefully(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("app.load_user_context", return_value=_ctx()), patch(
        "app.get_or_create_wallet", return_value={}
    ):
        # Real create_portal_session runs: empty customer id -> friendly
        # error, NOT a 500.
        resp = client.get("/billing/portal")
    assert resp.status_code == 302
    assert "portal_error=1" in resp.headers["Location"]
