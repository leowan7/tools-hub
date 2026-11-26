"""WeChat Pay top-up: PaymentIntent params, QR page, poll ownership, webhook credit-once."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from billing import wechat

pytestmark = pytest.mark.usefixtures("isolate_supabase")

USER = "u-wechat"
OTHER = "u-other"
STRIPE_HOSTED = {
    "image_url_png": "https://qr.stripe.com/test.png",
    "image_url_svg": "https://qr.stripe.com/test.svg",
    "hosted_instructions_url": "https://payments.stripe.com/wechat/instructions",
}


def _pi(user_id=USER, status="requires_action", pi_id="pi_wx_1"):
    return {
        "id": pi_id,
        "status": status,
        "currency": "cny",
        "amount": 3338,
        "metadata": {
            "user_id": user_id, "kind": "topup", "channel": "wechat_pay",
            "amount_usd": "5.00", "amount_cny": "33.38",
            "usd_cny_rate": "6.6757", "rate_date": "2026-11-25",
        },
        "next_action": {
            "type": "wechat_pay_display_qr_code",
            "wechat_pay_display_qr_code": {"data": "weixin://wxpay/bizpayurl?pr=TEST", **STRIPE_HOSTED},
        },
    }


class _FakePI:
    def __init__(self):
        self.created: list[dict] = []
        self.store: dict[str, dict] = {}

    def create(self, **kwargs):
        self.created.append(kwargs)
        pi = _pi()
        self.store[pi["id"]] = pi
        return pi

    def retrieve(self, pi_id):
        if pi_id not in self.store:
            raise RuntimeError("No such payment_intent")
        return self.store[pi_id]


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = SimpleNamespace(PaymentIntent=_FakePI())
    monkeypatch.setattr(wechat, "_stripe_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _fixed_rate(monkeypatch):
    monkeypatch.setattr(wechat, "usd_cny_rate", lambda: (Decimal("6.6757"), "2026-11-25"))


# --- quote + PaymentIntent params -------------------------------------------


def test_quote_rounds_cny_up_to_the_fen():
    quote, err = wechat.make_quote("5")
    assert err is None
    # 5 x 6.6757 = 33.3785 -> 33.38 (up)
    assert quote["amount_cny"] == "33.38"
    assert quote["amount_usd"] == "5.00"


def test_quote_reuses_topup_floor():
    quote, err = wechat.make_quote("1")
    assert quote is None and "minimum" in err


def test_quote_refuses_when_rate_unavailable(monkeypatch):
    monkeypatch.setattr(wechat, "usd_cny_rate", lambda: (None, None))
    quote, err = wechat.make_quote("20")
    assert quote is None and "unavailable" in err


def test_rate_outside_band_is_refused(monkeypatch):
    monkeypatch.undo()
    wechat._rate_cache.clear()

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"date": "2026-11-25", "rates": {"CNY": 66.757}}

    monkeypatch.setattr(wechat.requests, "get", lambda *a, **k: _Resp())
    assert wechat.usd_cny_rate() == (None, None)


def test_payment_intent_params(fake_stripe):
    quote, _ = wechat.make_quote("5")
    pi, err = wechat.create_wechat_payment_intent(USER, quote)
    assert err is None and pi["id"] == "pi_wx_1"
    args = fake_stripe.PaymentIntent.created[0]
    assert args["amount"] == 3338
    assert args["currency"] == "cny"
    assert args["payment_method_types"] == ["wechat_pay"]
    assert args["payment_method_data"] == {"type": "wechat_pay"}
    assert args["payment_method_options"] == {"wechat_pay": {"client": "web"}}
    assert args["confirm"] is True
    assert args["metadata"] == {
        "user_id": USER, "kind": "topup", "channel": "wechat_pay",
        "amount_usd": "5.00", "amount_cny": "33.38",
        "usd_cny_rate": "6.6757", "rate_date": "2026-11-25",
    }
    assert "automatic_tax" not in args


def test_expired_quote_is_refused(fake_stripe):
    quote, _ = wechat.make_quote("5")
    quote["quoted_at"] = int(time.time()) - wechat.QUOTE_TTL_SECONDS - 1
    pi, err = wechat.create_wechat_payment_intent(USER, quote)
    assert pi is None and "expired" in err
    assert fake_stripe.PaymentIntent.created == []


def test_qr_uses_data_never_hosted_urls():
    uri = wechat.qr_svg_data_uri(wechat.qr_payload(_pi()))
    assert uri.startswith("data:image/svg+xml")
    assert "stripe.com" not in uri


@pytest.mark.parametrize("status,state", [
    ("requires_action", "pending"), ("processing", "pending"),
    ("succeeded", "succeeded"), ("requires_payment_method", "failed"), ("canceled", "failed"),
])
def test_payment_state(status, state):
    assert wechat.payment_state({"status": status}) == state


# --- routes ------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _login(client, user_id=USER):
    with client.session_transaction() as sess:
        sess["user_email"] = "u@example.com"
        sess["user_id"] = user_id


def _ctx(user_id=USER):
    return SimpleNamespace(user_id=user_id, tier="free", email="u@example.com")


@pytest.fixture
def logged_in(client):
    with patch("blueprints.wallet.load_user_context", return_value=_ctx()), patch(
        "blueprints.wallet.get_or_create_wallet", return_value={"wallet_frozen": False}
    ):
        _login(client)
        yield client


def test_full_flow_renders_quote_then_inline_qr(logged_in, fake_stripe):
    resp = logged_in.post("/account/wallet/wechat/quote", data={"amount_usd": "5"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "33.38" in body and "$5.00" in body
    assert fake_stripe.PaymentIntent.created == []  # nothing charged at quote time

    resp = logged_in.post("/account/wallet/wechat", data={"amount_usd": "5.00", "amount_cny": "33.38"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/account/wallet/wechat")
    assert len(fake_stripe.PaymentIntent.created) == 1

    page = logged_in.get("/account/wallet/wechat").get_data(as_text=True)
    assert 'src="data:image/svg+xml' in page
    for url in STRIPE_HOSTED.values():
        assert url not in page
    assert "stripe.com" not in page
    assert "js.stripe.com" not in page


def test_create_without_quote_does_not_charge(logged_in, fake_stripe):
    resp = logged_in.post("/account/wallet/wechat")
    assert resp.status_code == 302 and "topup_error" in resp.headers["Location"]
    assert fake_stripe.PaymentIntent.created == []


def test_confirm_of_a_replaced_quote_does_not_charge(logged_in, fake_stripe):
    logged_in.post("/account/wallet/wechat/quote", data={"amount_usd": "5"})
    logged_in.post("/account/wallet/wechat/quote", data={"amount_usd": "100"})  # second tab
    resp = logged_in.post("/account/wallet/wechat", data={"amount_usd": "5.00", "amount_cny": "33.38"})
    assert "topup_error" in resp.headers["Location"]
    assert fake_stripe.PaymentIntent.created == []


def test_frozen_wallet_cannot_quote(client, fake_stripe):
    with patch("blueprints.wallet.load_user_context", return_value=_ctx()), patch(
        "blueprints.wallet.get_or_create_wallet", return_value={"wallet_frozen": True}
    ):
        _login(client)
        resp = client.post("/account/wallet/wechat/quote", data={"amount_usd": "5"})
    assert "wallet_frozen=1" in resp.headers["Location"]


def test_status_requires_login(client):
    resp = client.get("/account/wallet/wechat/status")
    assert resp.status_code in (302, 401)


def test_status_reads_session_pi_and_ignores_client_id(logged_in, fake_stripe):
    fake_stripe.PaymentIntent.store["pi_wx_1"] = _pi(status="succeeded")
    fake_stripe.PaymentIntent.store["pi_theirs"] = _pi(user_id=OTHER, pi_id="pi_theirs")
    # No PI in the session: a client-supplied id is not used.
    resp = logged_in.get("/account/wallet/wechat/status?pi=pi_theirs&payment_intent=pi_theirs")
    assert resp.status_code == 404
    with logged_in.session_transaction() as sess:
        sess["wechat_pi"] = "pi_wx_1"
    resp = logged_in.get("/account/wallet/wechat/status")
    assert resp.get_json() == {"status": "succeeded"}


def test_status_refuses_another_users_pi(logged_in, fake_stripe):
    fake_stripe.PaymentIntent.store["pi_theirs"] = _pi(user_id=OTHER, pi_id="pi_theirs")
    with logged_in.session_transaction() as sess:
        sess["wechat_pi"] = "pi_theirs"
    resp = logged_in.get("/account/wallet/wechat/status")
    assert resp.status_code == 404
    assert logged_in.get("/account/wallet/wechat").status_code == 302


def test_status_never_credits(logged_in, fake_stripe):
    fake_stripe.PaymentIntent.store["pi_wx_1"] = _pi(status="succeeded")
    with logged_in.session_transaction() as sess:
        sess["wechat_pi"] = "pi_wx_1"
    with patch("webhooks.stripe.top_up_wallet") as credit, patch(
        "shared.wallet.top_up_wallet"
    ) as credit2:
        logged_in.get("/account/wallet/wechat/status")
        logged_in.get("/account/wallet/wechat")
    credit.assert_not_called()
    credit2.assert_not_called()


# --- webhook -------------------------------------------------------------------


@pytest.fixture
def hook():
    from flask import Flask

    from tests.test_stripe_webhooks import _FakeClient
    from webhooks import stripe as stripe_webhook

    credits: list[dict] = []
    app = Flask(__name__)
    stripe_webhook.register_stripe_webhook(app)
    with patch.object(stripe_webhook, "get_service_client", return_value=_FakeClient()), patch.object(
        stripe_webhook, "top_up_wallet", side_effect=lambda **kw: credits.append(kw) or {"user_id": kw["user_id"]}
    ), patch.object(stripe_webhook, "_send_email_safe"):

        def post(event):
            with patch.object(stripe_webhook, "_verify_signature", return_value=event):
                return app.test_client().post(
                    "/webhooks/stripe", data=json.dumps(event), headers={"Stripe-Signature": "ok"}
                ).get_json()

        yield post, credits


def _succeeded(obj, event_id="evt_wx_1"):
    return {"id": event_id, "type": "payment_intent.succeeded", "data": {"object": obj}}


def test_webhook_credits_stored_usd_once_on_replay(hook):
    post, credits = hook
    obj = _pi(status="succeeded")
    first = post(_succeeded(obj))
    second = post(_succeeded(obj))
    assert first["status"] == "ok"
    assert second["status"] == "already_processed"
    assert len(credits) == 1
    # Credits the stored USD, not a conversion of the 33.38 CNY.
    assert credits[0]["amount_usd"] == Decimal("5.00")
    assert credits[0]["kind"] == "topup"
    assert credits[0]["stripe_payment_intent_id"] == "pi_wx_1"
    assert credits[0]["stripe_event_id"] == "evt_wx_1"


def test_webhook_still_ignores_card_topup_pi(hook):
    post, credits = hook
    card = {"id": "pi_card", "amount": 2000, "currency": "usd",
            "metadata": {"user_id": USER, "kind": "topup"}}
    assert post(_succeeded(card, "evt_card"))["status"] == "ignored"
    assert credits == []


def test_webhook_skips_wechat_marker_in_wrong_currency(hook):
    post, credits = hook
    obj = _pi(status="succeeded")
    obj["currency"] = "usd"
    assert post(_succeeded(obj, "evt_wx_usd"))["status"] == "skipped"
    assert credits == []


def test_topup_page_offers_card_and_wechat_choice(client):
    wallet = {"wallet_frozen": False, "balance_usd": 0, "auto_reload_enabled": False,
              "auto_reload_threshold_usd": 10, "auto_reload_amount_usd": 50,
              "auto_reload_monthly_cap_usd": 1000}
    with patch("blueprints.wallet.load_user_context", return_value=_ctx()), patch(
        "blueprints.wallet.get_or_create_wallet", return_value=wallet
    ):
        _login(client)
        page = client.get("/account/wallet/topup").get_data(as_text=True)
    assert 'name="pay_with" value="card" id="pay_with_card" checked' in page
    assert 'name="pay_with" value="wechat" id="pay_with_wechat"' in page
    assert 'formaction="/account/wallet/wechat/quote"' in page
    assert "Continue to checkout" in page and "Continue to WeChat Pay" in page
    assert "Charged in Chinese yuan" in page and "Charged in US dollars" in page
    assert "stripe.com" not in page
