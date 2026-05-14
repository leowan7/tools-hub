"""Unit tests for :mod:`billing.checkout`.

The wallet pivot ships a single Stripe product (``Tools-Hub Wallet
Top-Up``) with no fixed Price; each Checkout Session passes
``price_data`` inline with the user supplied amount. These tests cover
the contract by stubbing out the Stripe SDK at the ``_stripe_client``
boundary so the suite runs offline.

Coverage
--------

* ``create_topup_session`` returns ``{"id", "url"}`` on success.
* Amounts below ``WALLET_MIN_TOPUP_USD`` are rejected.
* Amounts above ``WALLET_MAX_TOPUP_USD`` are rejected.
* The Session is created with ``automatic_tax.enabled=true``.
* ``success_url`` and ``cancel_url`` are derived from the env base URL
  and the success URL carries the ``CHECKOUT_SESSION_ID`` placeholder so
  Agent E's ``/account/topup-complete`` endpoint can resolve the id.
* Metadata flows through so the webhook can credit the right wallet.
* ``save_payment_method=True`` adds the ``setup_future_usage`` block.
* ``retrieve_topup_session`` proxies through to Stripe and returns the
  minimum payload the success page needs.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Optional
from unittest.mock import patch

import pytest

import billing.checkout as checkout


USER_ID = "00000000-0000-0000-0000-000000000001"
USER_EMAIL = "leo@example.com"
PRODUCT_ID = "prod_test_wallet_topup"
BASE_URL = "https://tools.example.com"


# ---------------------------------------------------------------------------
# Fake Stripe SDK
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stand in for ``stripe.checkout.Session`` instances."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def __getattr__(self, name: str) -> Any:
        try:
            return self._fields[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, name: str) -> Any:
        return self._fields[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self._fields.get(name, default)


class _FakeStripeCheckout:
    """Captures the most recent ``Session.create`` payload."""

    def __init__(self) -> None:
        self.last_create_kwargs: Optional[dict] = None
        self.create_calls: int = 0
        self._next_id = 0

    def Session(self) -> "_FakeStripeCheckout":  # legacy accessor compatibility
        return self

    def _create(self, **kwargs: Any) -> _FakeSession:
        self.create_calls += 1
        self.last_create_kwargs = kwargs
        self._next_id += 1
        sid = f"cs_test_{self._next_id:06d}"
        return _FakeSession(
            id=sid,
            url=f"https://checkout.stripe.com/c/pay/{sid}",
            status="open",
            payment_status="unpaid",
            metadata=kwargs.get("metadata", {}),
        )


class _FakeSessionNamespace:
    def __init__(self, parent: _FakeStripeCheckout) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _FakeSession:
        return self._parent._create(**kwargs)

    def retrieve(self, session_id: str) -> _FakeSession:
        return _FakeSession(
            id=session_id,
            url=f"https://checkout.stripe.com/c/pay/{session_id}",
            status="complete",
            payment_status="paid",
            amount_total=2500,
            currency="usd",
            customer_email=USER_EMAIL,
            metadata={"user_id": USER_ID, "kind": "topup", "amount_usd": "25.00"},
            payment_intent="pi_test_123",
        )


class _FakeStripeModule:
    """Top level module stand in. Exposes ``checkout.Session``."""

    def __init__(self) -> None:
        self.api_key: Optional[str] = None
        self._checkout = _FakeStripeCheckout()
        self.checkout = type(
            "_CheckoutNamespace",
            (),
            {"Session": _FakeSessionNamespace(self._checkout)},
        )

    @property
    def last_create_kwargs(self) -> Optional[dict]:
        return self._checkout.last_create_kwargs

    @property
    def create_calls(self) -> int:
        return self._checkout.create_calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> _FakeStripeModule:
    """Replace ``billing.checkout._stripe_client`` with a fake module."""
    fake = _FakeStripeModule()
    fake.api_key = "sk_test_fake"

    def _factory():
        return fake

    monkeypatch.setattr(checkout, "_stripe_client", _factory)
    return fake


@pytest.fixture(autouse=True)
def _wallet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin env vars so tests are independent of the developer's shell."""
    monkeypatch.setenv("STRIPE_WALLET_TOPUP_PRODUCT_ID", PRODUCT_ID)
    monkeypatch.setenv("APP_BASE_URL", BASE_URL)
    monkeypatch.setenv("WALLET_MIN_TOPUP_USD", "20")
    monkeypatch.setenv("WALLET_MAX_TOPUP_USD", "5000")
    # Make sure the legacy APP_URL never wins over APP_BASE_URL.
    monkeypatch.delenv("APP_URL", raising=False)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_topup_session_returns_id_and_url(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("25.00")
    )
    assert err is None
    assert session is not None
    assert session["id"].startswith("cs_test_")
    assert session["url"].startswith("https://checkout.stripe.com/")
    assert session["amount_usd"] == "25.00"
    assert session["save_payment_method"] is False


def test_create_topup_session_accepts_int_amount(
    fake_stripe: _FakeStripeModule,
) -> None:
    session, err = checkout.create_topup_session(USER_ID, USER_EMAIL, 50)
    assert err is None and session is not None
    kwargs = fake_stripe.last_create_kwargs
    assert kwargs is not None
    unit_amount = kwargs["line_items"][0]["price_data"]["unit_amount"]
    assert unit_amount == 5000


def test_create_topup_session_accepts_decimal_string_amount(
    fake_stripe: _FakeStripeModule,
) -> None:
    session, err = checkout.create_topup_session(USER_ID, USER_EMAIL, "42.50")
    assert err is None and session is not None
    kwargs = fake_stripe.last_create_kwargs
    assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 4250


def test_create_topup_session_uses_inline_price_data(
    fake_stripe: _FakeStripeModule,
) -> None:
    """No fixed Stripe Price; the product is set via ``price_data.product``."""
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("30.00"))
    line_item = fake_stripe.last_create_kwargs["line_items"][0]
    assert "price" not in line_item, "must not reference a fixed Stripe Price"
    assert line_item["price_data"]["currency"] == "usd"
    assert line_item["price_data"]["product"] == PRODUCT_ID
    assert line_item["price_data"]["unit_amount"] == 3000
    assert line_item["quantity"] == 1


# ---------------------------------------------------------------------------
# Bounds enforcement
# ---------------------------------------------------------------------------


def test_amount_below_minimum_is_rejected(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("5.00")
    )
    assert session is None
    assert err is not None
    assert "minimum" in err.lower()
    assert fake_stripe.create_calls == 0


def test_amount_at_minimum_is_accepted(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("20.00")
    )
    assert err is None and session is not None
    assert fake_stripe.create_calls == 1


def test_amount_above_maximum_is_rejected(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("10000.00")
    )
    assert session is None
    assert err is not None
    assert "maximum" in err.lower()
    assert fake_stripe.create_calls == 0


def test_amount_at_maximum_is_accepted(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("5000.00")
    )
    assert err is None and session is not None
    assert fake_stripe.create_calls == 1


def test_non_positive_amount_is_rejected(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("0.00")
    )
    assert session is None and err is not None
    assert fake_stripe.create_calls == 0


def test_unparseable_amount_is_rejected(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(USER_ID, USER_EMAIL, "not-money")
    assert session is None and err is not None
    assert fake_stripe.create_calls == 0


# ---------------------------------------------------------------------------
# Tax + URLs
# ---------------------------------------------------------------------------


def test_stripe_tax_is_enabled_on_session(
    fake_stripe: _FakeStripeModule,
) -> None:
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25.00"))
    kwargs = fake_stripe.last_create_kwargs
    assert kwargs is not None
    assert kwargs.get("automatic_tax") == {"enabled": True}


def test_success_url_uses_env_base_and_session_placeholder(
    fake_stripe: _FakeStripeModule,
) -> None:
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25.00"))
    kwargs = fake_stripe.last_create_kwargs
    expected_success = (
        BASE_URL
        + "/account/topup-complete?session_id={CHECKOUT_SESSION_ID}"
    )
    assert kwargs["success_url"] == expected_success


def test_cancel_url_uses_env_base(fake_stripe: _FakeStripeModule) -> None:
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25.00"))
    kwargs = fake_stripe.last_create_kwargs
    assert kwargs["cancel_url"] == BASE_URL + "/account?topup=cancelled"


def test_base_url_trailing_slash_is_normalised(
    fake_stripe: _FakeStripeModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_BASE_URL", BASE_URL + "/")
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25.00"))
    kwargs = fake_stripe.last_create_kwargs
    assert kwargs["success_url"].startswith(BASE_URL + "/")
    # Two slashes between host and path would be a routing bug.
    assert "//account/topup-complete" not in kwargs["success_url"].replace(
        "https://", ""
    )


# ---------------------------------------------------------------------------
# Metadata + save_payment_method
# ---------------------------------------------------------------------------


def test_metadata_carries_user_id_and_amount(
    fake_stripe: _FakeStripeModule,
) -> None:
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25.00"))
    kwargs = fake_stripe.last_create_kwargs
    metadata = kwargs["metadata"]
    assert metadata["user_id"] == USER_ID
    assert metadata["kind"] == "topup"
    assert metadata["amount_usd"] == "25.00"
    assert "save_pm" not in metadata


def test_customer_email_is_prefilled(fake_stripe: _FakeStripeModule) -> None:
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25.00"))
    assert fake_stripe.last_create_kwargs["customer_email"] == USER_EMAIL


def test_mode_is_payment_not_subscription(
    fake_stripe: _FakeStripeModule,
) -> None:
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25.00"))
    assert fake_stripe.last_create_kwargs["mode"] == "payment"


def test_save_payment_method_sets_setup_future_usage(
    fake_stripe: _FakeStripeModule,
) -> None:
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("25.00"), save_payment_method=True
    )
    assert err is None and session is not None
    assert session["save_payment_method"] is True
    kwargs = fake_stripe.last_create_kwargs
    pi_data = kwargs.get("payment_intent_data")
    assert pi_data is not None
    assert pi_data["setup_future_usage"] == "off_session"
    assert pi_data["metadata"]["save_pm"] == "true"
    assert kwargs["metadata"]["save_pm"] == "true"


# ---------------------------------------------------------------------------
# Misconfiguration + bad inputs
# ---------------------------------------------------------------------------


def test_missing_user_id_is_rejected(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session("", USER_EMAIL, Decimal("25"))
    assert session is None
    assert err is not None
    assert fake_stripe.create_calls == 0


def test_missing_user_email_is_rejected(fake_stripe: _FakeStripeModule) -> None:
    session, err = checkout.create_topup_session(USER_ID, "", Decimal("25"))
    assert session is None
    assert err is not None
    assert fake_stripe.create_calls == 0


def test_missing_product_id_returns_error(
    fake_stripe: _FakeStripeModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STRIPE_WALLET_TOPUP_PRODUCT_ID", raising=False)
    monkeypatch.delenv("STRIPE_TOPUP_PRODUCT_ID", raising=False)
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("25")
    )
    assert session is None
    assert err is not None
    assert "STRIPE_WALLET_TOPUP_PRODUCT_ID" in err
    assert fake_stripe.create_calls == 0


def test_missing_stripe_secret_key_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Do not install the fake_stripe fixture; rely on the real client
    # factory, which should bail on the empty API key.
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("25")
    )
    assert session is None
    assert err is not None


def test_alt_product_env_name_is_accepted(
    fake_stripe: _FakeStripeModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``STRIPE_TOPUP_PRODUCT_ID`` is accepted when the wallet name is unset."""
    monkeypatch.delenv("STRIPE_WALLET_TOPUP_PRODUCT_ID", raising=False)
    monkeypatch.setenv("STRIPE_TOPUP_PRODUCT_ID", "prod_alt_name")
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25"))
    line_item = fake_stripe.last_create_kwargs["line_items"][0]
    assert line_item["price_data"]["product"] == "prod_alt_name"


def test_override_arguments_take_precedence(
    fake_stripe: _FakeStripeModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test arguments win over env vars (used for harness specific runs)."""
    session, err = checkout.create_topup_session(
        USER_ID,
        USER_EMAIL,
        Decimal("25"),
        product_id="prod_override",
        base_url="https://override.example/",
        min_topup_usd=Decimal("10"),
        max_topup_usd=Decimal("100"),
    )
    assert err is None and session is not None
    kwargs = fake_stripe.last_create_kwargs
    assert kwargs["line_items"][0]["price_data"]["product"] == "prod_override"
    assert kwargs["success_url"].startswith("https://override.example/")


def test_override_min_max_enforced(
    fake_stripe: _FakeStripeModule,
) -> None:
    session, err = checkout.create_topup_session(
        USER_ID,
        USER_EMAIL,
        Decimal("150"),
        min_topup_usd=Decimal("10"),
        max_topup_usd=Decimal("100"),
    )
    assert session is None
    assert err is not None
    assert "maximum" in err.lower()


def test_stripe_create_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised exception in ``Session.create`` must be reported as an error."""

    class _Boom:
        api_key = "sk_test_fake"

        class checkout:  # noqa: N801
            class Session:  # noqa: N801
                @staticmethod
                def create(**_kwargs: Any):
                    raise RuntimeError("network down")

    monkeypatch.setattr(checkout, "_stripe_client", lambda: _Boom())
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("25")
    )
    assert session is None
    assert err is not None


# ---------------------------------------------------------------------------
# Defaults when env vars are unset
# ---------------------------------------------------------------------------


def test_default_min_topup_falls_back_to_module(
    fake_stripe: _FakeStripeModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WALLET_MIN_TOPUP_USD", raising=False)
    from shared.wallet import MIN_TOPUP_USD

    # An amount one cent below the module default must be rejected.
    below = (MIN_TOPUP_USD - Decimal("0.01")).quantize(Decimal("0.01"))
    session, err = checkout.create_topup_session(USER_ID, USER_EMAIL, below)
    assert session is None and err is not None and "minimum" in err.lower()


def test_default_max_topup_is_five_thousand(
    fake_stripe: _FakeStripeModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WALLET_MAX_TOPUP_USD", raising=False)
    session, err = checkout.create_topup_session(
        USER_ID, USER_EMAIL, Decimal("5000.01")
    )
    assert session is None and err is not None
    assert "maximum" in err.lower()


def test_base_url_falls_back_to_app_url(
    fake_stripe: _FakeStripeModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    monkeypatch.setenv("APP_URL", "https://legacy.example.com")
    checkout.create_topup_session(USER_ID, USER_EMAIL, Decimal("25"))
    kwargs = fake_stripe.last_create_kwargs
    assert kwargs["success_url"].startswith("https://legacy.example.com/")


# ---------------------------------------------------------------------------
# retrieve_topup_session
# ---------------------------------------------------------------------------


def test_retrieve_topup_session_returns_summary(
    fake_stripe: _FakeStripeModule,
) -> None:
    data, err = checkout.retrieve_topup_session("cs_test_abc")
    assert err is None
    assert data is not None
    assert data["id"] == "cs_test_abc"
    assert data["status"] == "complete"
    assert data["payment_status"] == "paid"
    assert data["amount_total"] == 2500
    assert data["currency"] == "usd"
    assert data["metadata"]["user_id"] == USER_ID
    assert data["payment_intent"] == "pi_test_123"


def test_retrieve_topup_session_requires_session_id() -> None:
    data, err = checkout.retrieve_topup_session("")
    assert data is None and err is not None


def test_retrieve_topup_session_no_stripe_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "_stripe_client", lambda: None)
    data, err = checkout.retrieve_topup_session("cs_test_abc")
    assert data is None and err is not None


# ---------------------------------------------------------------------------
# create_portal_session
# ---------------------------------------------------------------------------


def test_create_portal_session_returns_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Portal:
        url = "https://billing.stripe.com/p/session/test_123"

    class _BillingPortal:
        class Session:  # noqa: N801
            @staticmethod
            def create(**_kwargs: Any) -> "_Portal":
                return _Portal()

    fake_stripe_module = type(
        "_S",
        (),
        {
            "api_key": "sk_test_fake",
            "billing_portal": _BillingPortal(),
        },
    )()
    monkeypatch.setattr(checkout, "_stripe_client", lambda: fake_stripe_module)
    url, err = checkout.create_portal_session(
        customer_id="cus_test_123",
        return_url="https://tools.example.com/account",
    )
    assert err is None
    assert url == "https://billing.stripe.com/p/session/test_123"


def test_create_portal_session_requires_customer_id() -> None:
    url, err = checkout.create_portal_session(
        customer_id="",
        return_url="https://tools.example.com/account",
    )
    assert url is None and err is not None
