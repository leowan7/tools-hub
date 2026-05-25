"""Jinja render smoke tests for wallet templates and pricing.

Closes Wave 4 Fix 8. Each test boots the real Flask app, pushes a
request context (with realistic query args and session state where
the template depends on it), and calls ``render_template`` with a
fixture context that mirrors what the production route passes. A
failure here means a contract mismatch between an app.py route and
a template, or a stray Jinja syntax error.

The Wave 2 contract mismatches that landed on main and only got
caught at cross diff review (sections 4.1, 4.2, 4.3 of
``docs/WAVE2-REVIEW.md``) would have failed these tests at CI.

Templates covered:

  templates/wallet/overview.html       (account dashboard)
  templates/wallet/topup.html          (4 variants: standalone, gate,
                                        success, error)
  templates/wallet/transactions.html   (ledger view, with + without rows)
  templates/pricing.html               (logged in + anonymous)

The partial ``templates/wallet/_partials.html`` is rendered transitively
when topup forms include it; it does not have its own dedicated test
yet because it is imported as macros rather than rendered standalone.
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone

import pytest
from flask import render_template


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    """Boot the Flask app with the wallet feature flags on.

    Mirrors the fixture in ``tests/test_wallet_api.py`` so endpoint
    registrations (``url_for('tool_form', ...)``, ``url_for('signup')``,
    ``url_for('index')``, ``url_for('wallet_topup')``) resolve.
    """
    monkeypatch.setenv("FLAG_TOOL_MPNN", "on")
    monkeypatch.setenv("FLAG_TOOL_BINDCRAFT", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _wallet_fixture(balance=42.50, frozen=False, auto_reload_on=False):
    """Realistic wallet dict matching shared/wallet.py shape."""
    return {
        "user_id": "u-smoke",
        "balance_usd": Decimal(str(balance)),
        "daily_spend_cap_usd": Decimal("200.00"),
        "auto_reload_enabled": auto_reload_on,
        "auto_reload_threshold_usd": Decimal("10.00"),
        "auto_reload_amount_usd": Decimal("50.00"),
        "auto_reload_monthly_cap_usd": Decimal("1000.00"),
        "wallet_frozen": frozen,
        "spent_today_usd": Decimal("3.25"),
        "spent_30d_usd": Decimal("47.10"),
        "signup_credit_used_usd": Decimal("5.00"),
        "stripe_customer_id": None,
    }


def _transaction_fixture(kind="topup", amount=20.00, balance_after=42.50):
    """One ledger row matching shared/wallet.py shape."""
    return {
        "id": "tx-1",
        "kind": kind,
        "amount_usd": Decimal(str(amount)),
        "balance_after_usd": Decimal(str(balance_after)),
        "created_at": datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc),
        "tool_slug": "mpnn" if kind in ("hold", "charge") else None,
        "job_id": "job-1" if kind in ("hold", "charge") else None,
        "parent_tx_id": None,
        "note": None,
        "stripe_event_id": "evt_test_1" if kind == "topup" else None,
    }


# ---------------------------------------------------------------------------
# wallet/overview.html
# ---------------------------------------------------------------------------


class TestWalletOverviewTemplate:
    def test_overview_renders_with_realistic_context(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[
                    _transaction_fixture("topup"),
                    _transaction_fixture("charge", -1.25, 41.25),
                ],
                user_email="u@example.com",
            )
        assert "Current balance" in html
        assert "$42.50" in html
        assert "Top up" in html

    def test_overview_renders_with_empty_transactions(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "Wallet" in html

    def test_overview_shows_topup_success_banner_when_query_arg_set(self, app):
        """request.args.get('topup_success') controls the banner."""
        with app.test_request_context("/account/wallet?topup_success=1"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(),
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "Top up confirmed" in html

    def test_overview_shows_frozen_warning_when_wallet_frozen(self, app):
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=_wallet_fixture(frozen=True),
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "Wallet frozen" in html

    def test_overview_renders_without_signup_credit_used_usd_key(self, app):
        """The user_wallets schema does not have signup_credit_used_usd.

        The Session 8 Pass 6 surfaced that overview.html was reading this
        key directly and 500'ing on every real wallet because the column
        does not exist on user_wallets. The route does not inject it
        either. The template must tolerate the missing key.
        """
        wallet_no_signup_field = _wallet_fixture()
        wallet_no_signup_field.pop("signup_credit_used_usd", None)
        with app.test_request_context("/account/wallet"):
            html = render_template(
                "wallet/overview.html",
                wallet=wallet_no_signup_field,
                recent_transactions=[],
                user_email="u@example.com",
            )
        assert "signup balance available" in html
        assert "$5.00" in html


# ---------------------------------------------------------------------------
# wallet/topup.html (4 variants)
# ---------------------------------------------------------------------------


class TestWalletTopupTemplate:
    def test_standalone_topup_form_renders(self, app):
        """No deficit_usd, no topup_success, no topup_error.

        Renders the bare 'Pick an amount' form, the auto reload panel,
        and the cost table.
        """
        with app.test_request_context("/account/wallet/topup"):
            html = render_template(
                "wallet/topup.html",
                wallet=_wallet_fixture(),
                min_topup_usd=Decimal("20.00"),
                next_url=None,
                topup_action_url="/account/wallet/checkout",
                topup_error=None,
            )
        assert "Top up your wallet" in html
        assert "Pick an amount" in html
        assert "Continue to checkout" in html
        assert "Auto reload" in html
        assert "wallet-topup-form" in html

    def test_gate_flow_with_deficit_shows_top_up_and_run_cta(self, app):
        """Decorator gate render: deficit_usd + next_url present.

        Mirrors what app.py:_render_topup_gate passes. Tests the contract
        from WAVE2-REVIEW.md section 4.1.
        """
        with app.test_request_context("/tools/mpnn"):
            html = render_template(
                "wallet/topup.html",
                wallet=_wallet_fixture(balance=2.00),
                deficit_usd=Decimal("15.50"),
                estimate_usd=Decimal("17.50"),
                balance_usd=Decimal("2.00"),
                hard_cap_usd=Decimal("100.00"),
                suggested_amount=20,
                min_topup_usd=Decimal("20.00"),
                next_url="/tools/mpnn",
                gate_reason="insufficient_balance",
                tool_slug="mpnn",
                self_serve_ceiling_usd=Decimal("500.00"),
            )
        assert "Top up and run" in html
        assert "$15.50" in html
        assert "Back to the form" in html

    def test_success_state_renders_receipt_and_return_tool_cta(self, app):
        """After Stripe Checkout returns cleanly.

        Tests the Fix 7 success branch.
        """
        with app.test_request_context(
            "/account/topup-complete?session_id=cs_test_1"
        ):
            html = render_template(
                "wallet/topup.html",
                topup_success=True,
                stripe_session={
                    "id": "cs_test_1",
                    "amount_total": 2300,
                    "currency": "usd",
                    "status": "complete",
                    "payment_status": "paid",
                    "metadata": {"user_id": "u-smoke"},
                },
                wallet=_wallet_fixture(balance=68.00),
                return_tool="mpnn",
                return_tool_url="/tools/mpnn",
            )
        assert "Top up complete" in html
        assert "$23.00" in html
        assert "New balance $68.00" in html
        assert "Return to mpnn" in html
        assert "cs_test_1" in html
        assert "Pick an amount" not in html, (
            "success state must not render the top up form"
        )

    def test_success_state_without_return_tool_shows_browse_tools_cta(self, app):
        with app.test_request_context(
            "/account/topup-complete?session_id=cs_test_2"
        ):
            html = render_template(
                "wallet/topup.html",
                topup_success=True,
                stripe_session={
                    "id": "cs_test_2",
                    "amount_total": 5000,
                    "currency": "usd",
                    "status": "complete",
                    "payment_status": "paid",
                },
                wallet=_wallet_fixture(balance=92.50),
                return_tool=None,
                return_tool_url=None,
            )
        assert "Top up complete" in html
        assert "View wallet" in html
        assert "Browse tools" in html

    def test_error_state_renders_banner_above_form(self, app):
        """After Stripe Checkout fails or session lookup errors.

        Tests the Fix 7 error branch. The form is preserved so the
        user can retry.
        """
        with app.test_request_context(
            "/account/topup-complete?session_id=bad"
        ):
            html = render_template(
                "wallet/topup.html",
                topup_error=(
                    "Could not validate the Stripe session. The webhook "
                    "still credits the wallet when payment clears."
                ),
                wallet=_wallet_fixture(),
                return_tool="bindcraft",
            )
        assert "Top up did not complete" in html
        assert "Could not validate the Stripe session" in html
        # Form is still there for retry
        assert "Pick an amount" in html
        assert "Continue to checkout" in html
        # Return to tool link is rendered
        assert "bindcraft" in html


# ---------------------------------------------------------------------------
# wallet/transactions.html
# ---------------------------------------------------------------------------


class TestWalletTransactionsTemplate:
    def test_transactions_renders_with_rows(self, app):
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=[
                    _transaction_fixture("signup_credit", 5.00, 5.00),
                    _transaction_fixture("topup", 50.00, 55.00),
                    _transaction_fixture("hold", -3.00, 52.00),
                    _transaction_fixture("charge", -2.40, 49.60),
                    _transaction_fixture("hold_release", 0.60, 50.20),
                ],
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=5,
            )
        assert "Transaction history" in html
        assert "Current balance" in html
        assert "5 entries" in html

    def test_transactions_renders_with_kind_filter(self, app):
        with app.test_request_context(
            "/account/wallet/transactions?kind=topup"
        ):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=[_transaction_fixture("topup")],
                filter_kind="topup",
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=1,
            )
        assert "Transaction history" in html

    def test_transactions_renders_with_empty_list(self, app):
        with app.test_request_context("/account/wallet/transactions"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(balance=0),
                transactions=[],
                filter_kind=None,
                page=1,
                page_size=50,
                has_next=False,
                has_prev=False,
                total_count=0,
            )
        assert "Transaction history" in html

    def test_transactions_renders_with_pagination(self, app):
        with app.test_request_context("/account/wallet/transactions?page=2"):
            html = render_template(
                "wallet/transactions.html",
                wallet=_wallet_fixture(),
                transactions=[_transaction_fixture("topup")],
                filter_kind=None,
                page=2,
                page_size=50,
                has_next=True,
                has_prev=True,
                total_count=125,
            )
        assert "Transaction history" in html


# ---------------------------------------------------------------------------
# pricing.html
# ---------------------------------------------------------------------------


class TestPricingTemplate:
    def test_pricing_renders_for_logged_in_user(self, app):
        """session.user_email set => 'Top up your wallet' CTA."""
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_email"] = "u@example.com"
        with app.test_request_context("/pricing"):
            from flask import session as flask_session
            flask_session["user_email"] = "u@example.com"
            html = render_template("pricing.html")
        assert "Pay for the compute you use" in html
        assert "Top up your wallet" in html

    def test_pricing_renders_for_anonymous_user(self, app):
        """No session.user_email => signup CTA references $5 wallet balance."""
        with app.test_request_context("/pricing"):
            html = render_template("pricing.html")
        assert "Pay for the compute you use" in html
        assert "Start with $5 in your wallet" in html
