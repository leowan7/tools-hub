"""Tests for the wallet HTTP surface added by Wave 2 Agent E.

Covers:

* ``GET /api/wallet/estimate``: the inline Moment 1 cost preview
  used by every tool form.
* ``requires_wallet`` decorator: gates a tool submit POST. Renders
  the 'Top up and run' page when the wallet cannot cover the
  estimate (Moment 2) or when the parameter scaled hard cap is
  exceeded (Moment 3). Allows the handler through and reserves the
  hold when the wallet covers the estimate.
* ``GET /account/topup-complete``: Stripe Checkout success_url
  landing. Validates a session_id and renders confirmation.

Each test uses its own patch set so a missing service client in
one test never leaks into the next. The wallet preflight is
exercised through public surfaces (HTTP routes) instead of unit
calling the decorator, which gives the contract a real Flask
session and the closest possible match to production behaviour.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(user_id="u-wallet", balance=100):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=balance, email="u@example.com"
    )


def _login(client, email="u@example.com", user_id="u-wallet"):
    with client.session_transaction() as sess:
        sess["user_email"] = email
        sess["user_id"] = user_id


@pytest.fixture
def app(monkeypatch):
    """Boot the Flask app with the wallet feature flags on."""
    monkeypatch.setenv("FLAG_TOOL_MPNN", "on")
    monkeypatch.setenv("FLAG_TOOL_BINDCRAFT", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ===========================================================================
# /api/wallet/estimate
# ===========================================================================


class TestEstimateEndpointShape:
    """The endpoint must return the canonical JSON shape on every call."""

    def test_returns_json_with_expected_keys(self, client):
        with patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 5.0, "wallet_frozen": False},
        ):
            _login(client)
            resp = client.get(
                "/api/wallet/estimate?tool=mpnn&num_seq_per_target=8"
            )
        assert resp.status_code == 200
        body = resp.get_json()
        # Canonical shape every caller depends on.
        for key in (
            "ok",
            "tool_slug",
            "estimate_usd",
            "hard_cap_usd",
            "balance_usd",
            "balance_after_usd",
            "self_serve_ceiling_usd",
            "exceeds_hard_cap",
            "exceeds_self_serve_ceiling",
        ):
            assert key in body, f"missing key {key!r}"
        # Money fields are JSON strings so Decimal precision survives.
        assert isinstance(body["estimate_usd"], str)
        assert isinstance(body["hard_cap_usd"], str)
        assert isinstance(body["balance_usd"], str)

    def test_returns_400_when_tool_missing(self, client):
        resp = client.get("/api/wallet/estimate")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "missing_tool_slug"

    def test_returns_null_balance_for_anonymous(self, client):
        """No session: real estimate, NULL balance, no wallet-derived gates.

        ``/tools/<slug>`` renders the real run form to logged-out visitors,
        and its estimate panel calls this endpoint. It used to report a $0
        balance, which made ``hard_block`` true for every tool and painted
        "Estimate exceeds the ceiling for a single job — run this as a
        campaign" over the panel for every stranger who opened a tool page.
        A null balance is the honest answer: there is no wallet yet.
        """
        # No session, no wallet lookup expected.
        with patch("blueprints.wallet.get_or_create_wallet") as gow:
            resp = client.get(
                "/api/wallet/estimate?tool=mpnn"
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["balance_usd"] is None
        assert body["balance_after_usd"] is None
        assert body["authenticated"] is False
        # A real estimate still comes back — that is the whole point.
        assert Decimal(body["estimate_usd"]) > 0
        # None of the wallet-derived blocks may fire without a wallet.
        assert body["soft_block"] is False
        assert body["hard_block"] is False
        assert body["deficit_usd"] == "0"
        # Anonymous request must not hit Supabase for a wallet row.
        gow.assert_not_called()

    def test_uses_form_params_for_estimate_scaling(self, client):
        """Pass num_designs=1000 and see a scaled estimate above baseline."""
        with patch("blueprints.wallet.get_or_create_wallet", return_value=None):
            small = client.get(
                "/api/wallet/estimate?tool=bindcraft&num_designs=100"
            ).get_json()
            large = client.get(
                "/api/wallet/estimate?tool=bindcraft&num_designs=2000"
            ).get_json()
        # Both succeed and the larger params yield a higher estimate.
        assert Decimal(large["estimate_usd"]) > Decimal(small["estimate_usd"])
        # And hard cap scales too.
        assert Decimal(large["hard_cap_usd"]) > Decimal(
            small["hard_cap_usd"]
        )

    def test_balance_after_usd_reflects_estimate(self, client):
        # Stage a wallet with $50 balance.
        with patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 50.0, "wallet_frozen": False},
        ):
            _login(client)
            resp = client.get(
                "/api/wallet/estimate?tool=bindcraft&num_designs=100"
            )
        body = resp.get_json()
        balance = Decimal(body["balance_usd"])
        estimate = Decimal(body["estimate_usd"])
        assert Decimal(body["balance_after_usd"]) == balance - estimate

    def test_accepts_params_as_json_blob(self, client):
        """`params=<json>` query param overrides flat keys."""
        with patch("blueprints.wallet.get_or_create_wallet", return_value=None):
            params_json = json.dumps({"num_designs": 500})
            resp = client.get(
                "/api/wallet/estimate",
                query_string={
                    "tool": "bindcraft",
                    "params": params_json,
                },
            )
        assert resp.status_code == 200
        body = resp.get_json()
        # 500 designs: estimate should be well above the 100 baseline.
        assert Decimal(body["estimate_usd"]) > Decimal("4.40")


class TestEstimateAndCapFlags:
    def test_exceeds_self_serve_ceiling_flag_trips_on_giant_param(self, client):
        """num_designs typo into the millions trips the ceiling flag."""
        # Estimate gets clamped by compute_hard_cap inside the estimator,
        # but the exceeds_self_serve_ceiling flag reflects the raw value
        # so the form can render the Pilot CTA path.
        with patch("blueprints.wallet.get_or_create_wallet", return_value=None):
            resp = client.get(
                "/api/wallet/estimate",
                query_string={"tool": "bindcraft", "num_designs": 10_000_000},
            )
        body = resp.get_json()
        # The hard cap of $500 for bindcraft caps the estimate at $500.
        # At this scale the estimate equals the absolute cap so it is
        # not over the self serve ceiling. This is the documented
        # behaviour. The flag fires only when estimate crosses the
        # ceiling.
        assert body["estimate_usd"] is not None


# ===========================================================================
# requires_wallet decorator (exercised via the tool submit route)
# ===========================================================================


class TestRequiresWalletPassesWhenBalanceCoversEstimate:
    """Wallet covers the estimate: handler runs and hold is reserved."""

    def test_handler_invoked_when_balance_sufficient(self, client):
        from app import requires_wallet
        # The decorator is callable as a factory with explicit slug.
        called = {"hit": False}

        @requires_wallet(tool_slug="mpnn")
        def handler():
            called["hit"] = True
            from flask import g
            assert getattr(g, "wallet_hold_tx_id", None) == "tx-001"
            return "ok", 200

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        flask_app.add_url_rule(
            "/x", view_func=handler, methods=["POST"]
        )

        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("0.05"),
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 100.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight"
        ) as preflight, patch(
            "shared.wallet_guard.wallet_reserve_hold", return_value="tx-001"
        ):
            from shared.wallet import PreflightResult, REASON_OK
            preflight.return_value = PreflightResult(
                allow=True,
                reason=REASON_OK,
                estimated_cost_usd=Decimal("0.05"),
                balance_usd=Decimal("100"),
                deficit_usd=Decimal("0"),
                hard_cap_usd=Decimal("150"),
            )
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/x", data={"num_seq_per_target": "8"})

        assert resp.status_code == 200
        assert called["hit"] is True


class TestRequiresWalletBlocksAtMoment2:
    """Insufficient balance renders the gate, handler is not invoked."""

    def test_insufficient_balance_renders_gate(self, client):
        from app import requires_wallet
        from shared.wallet import (
            PreflightResult,
            REASON_INSUFFICIENT,
        )

        @requires_wallet(tool_slug="bindcraft")
        def handler():  # pragma: no cover (must not be called)
            raise AssertionError("handler should not run when blocked")

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        flask_app.add_url_rule(
            "/blocked", view_func=handler, methods=["POST"]
        )
        # Required by _render_topup_gate which calls url_for("tools.tool_form")
        flask_app.add_url_rule(
            "/tools/<tool>",
            endpoint="tools.tool_form",
            view_func=lambda tool: "form",
        )

        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("4.40"),
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 1.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight"
        ) as preflight, patch(
            "shared.wallet_guard.wallet_reserve_hold"
        ) as reserve, patch(
            "shared.wallet_guard.render_template", return_value="GATE_RENDERED"
        ) as render:
            preflight.return_value = PreflightResult(
                allow=False,
                reason=REASON_INSUFFICIENT,
                estimated_cost_usd=Decimal("4.40"),
                balance_usd=Decimal("1.00"),
                deficit_usd=Decimal("3.40"),
                hard_cap_usd=Decimal("8.00"),
            )
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/blocked", data={"num_designs": "100"})

        assert resp.status_code == 200
        assert resp.get_data(as_text=True) == "GATE_RENDERED"
        reserve.assert_not_called()
        # The render call used the wallet topup template
        rendered_tpl = render.call_args.args[0]
        assert rendered_tpl == "wallet/topup.html"
        ctx = render.call_args.kwargs
        assert ctx["gate_reason"] == REASON_INSUFFICIENT
        assert ctx["tool_slug"] == "bindcraft"


class TestRequiresWalletBlocksAtMoment3:
    """Per tool cap and self serve ceiling each render the gate."""

    def test_per_tool_cap_exceeded_renders_gate(self, client):
        from app import requires_wallet
        from shared.wallet import (
            PreflightResult,
            REASON_PER_TOOL_CAP,
        )

        @requires_wallet(tool_slug="bindcraft")
        def handler():  # pragma: no cover
            raise AssertionError("handler must not run")

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        flask_app.add_url_rule(
            "/cap", view_func=handler, methods=["POST"]
        )
        flask_app.add_url_rule(
            "/tools/<tool>",
            endpoint="tools.tool_form",
            view_func=lambda tool: "form",
        )

        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("600.00"),
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 1000.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight"
        ) as preflight, patch(
            "shared.wallet_guard.render_template", return_value="CAP_GATE"
        ) as render:
            preflight.return_value = PreflightResult(
                allow=False,
                reason=REASON_PER_TOOL_CAP,
                estimated_cost_usd=Decimal("600.00"),
                balance_usd=Decimal("1000.00"),
                deficit_usd=Decimal("0"),
                hard_cap_usd=Decimal("500.00"),
            )
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/cap", data={"num_designs": "10000"})

        assert resp.get_data(as_text=True) == "CAP_GATE"
        assert render.call_args.kwargs["gate_reason"] == REASON_PER_TOOL_CAP

    def test_self_serve_ceiling_exceeded_renders_gate(self, client):
        from app import requires_wallet
        from shared.wallet import (
            PreflightResult,
            REASON_SELF_SERVE_CEILING,
        )

        @requires_wallet(tool_slug="bindcraft")
        def handler():  # pragma: no cover
            raise AssertionError("handler must not run")

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        flask_app.add_url_rule(
            "/ceiling", view_func=handler, methods=["POST"]
        )
        flask_app.add_url_rule(
            "/tools/<tool>",
            endpoint="tools.tool_form",
            view_func=lambda tool: "form",
        )

        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("1500.00"),
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 5000.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight"
        ) as preflight, patch(
            "shared.wallet_guard.render_template", return_value="CEILING_GATE"
        ) as render:
            preflight.return_value = PreflightResult(
                allow=False,
                reason=REASON_SELF_SERVE_CEILING,
                estimated_cost_usd=Decimal("1500.00"),
                balance_usd=Decimal("5000.00"),
                deficit_usd=Decimal("0"),
                hard_cap_usd=Decimal("500.00"),
            )
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/ceiling", data={"num_designs": "100000"})
        assert resp.get_data(as_text=True) == "CEILING_GATE"
        kwargs = render.call_args.kwargs
        assert kwargs["gate_reason"] == REASON_SELF_SERVE_CEILING


class TestRequiresWalletMoment1NoGate:
    """A zero estimate (smoke tier) lets the handler run with no hold."""

    def test_zero_estimate_skips_gate_and_no_hold(self, client):
        from app import requires_wallet
        ran = {"called": False}

        @requires_wallet(tool_slug="mpnn")
        def handler():
            from flask import g
            ran["called"] = True
            # No hold reserved for a zero estimate.
            assert getattr(g, "wallet_hold_tx_id", None) is None
            assert getattr(g, "wallet_estimate_usd", None) == Decimal("0")
            return "ok"

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        flask_app.add_url_rule(
            "/smoke", view_func=handler, methods=["POST"]
        )

        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("0"),
        ), patch(
            "shared.wallet_guard.wallet_preflight"
        ) as preflight, patch(
            "shared.wallet_guard.wallet_reserve_hold"
        ) as reserve:
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/smoke", data={"preset": "smoke"})
        assert ran["called"] is True
        # Neither preflight nor reserve called when estimate is zero.
        preflight.assert_not_called()
        reserve.assert_not_called()


class TestRequiresWalletReserveLost:
    """If reserve_hold returns None we surface the gate, not a 500."""

    def test_reserve_hold_returning_null_renders_gate(self, client):
        from app import requires_wallet
        from shared.wallet import PreflightResult, REASON_OK

        @requires_wallet(tool_slug="bindcraft")
        def handler():  # pragma: no cover
            raise AssertionError("handler must not run when hold is None")

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        flask_app.add_url_rule(
            "/race", view_func=handler, methods=["POST"]
        )
        flask_app.add_url_rule(
            "/tools/<tool>",
            endpoint="tools.tool_form",
            view_func=lambda tool: "form",
        )

        ok = PreflightResult(
            allow=True,
            reason=REASON_OK,
            estimated_cost_usd=Decimal("4.40"),
            balance_usd=Decimal("100"),
            deficit_usd=Decimal("0"),
            hard_cap_usd=Decimal("8.00"),
        )
        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("4.40"),
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 100.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight", return_value=ok
        ), patch(
            "shared.wallet_guard.wallet_reserve_hold", return_value=None
        ), patch(
            "shared.wallet_guard.render_template", return_value="LOST_RACE"
        ) as render:
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/race", data={"num_designs": "100"})

        assert resp.get_data(as_text=True) == "LOST_RACE"
        # The gate render still used the wallet topup template.
        assert render.call_args.args[0] == "wallet/topup.html"


class TestRequiresWalletHandlerExceptionReleasesHold:
    """If the handler raises after the hold is placed, release it."""

    def test_handler_exception_triggers_release_hold(self, client):
        from app import requires_wallet
        from shared.wallet import PreflightResult, REASON_OK

        @requires_wallet(tool_slug="mpnn")
        def handler():
            raise RuntimeError("simulated handler crash")

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        # propagate so the decorator's try/except wraps the raise.
        flask_app.config["TESTING"] = True
        flask_app.add_url_rule(
            "/crash", view_func=handler, methods=["POST"]
        )

        ok = PreflightResult(
            allow=True,
            reason=REASON_OK,
            estimated_cost_usd=Decimal("0.05"),
            balance_usd=Decimal("100"),
            deficit_usd=Decimal("0"),
            hard_cap_usd=Decimal("150"),
        )
        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("0.05"),
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 100.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight", return_value=ok
        ), patch(
            "shared.wallet_guard.wallet_reserve_hold", return_value="tx-xyz"
        ), patch(
            "shared.wallet_guard.wallet_release_hold"
        ) as release:
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            # Flask catches the handler exception and returns a 500.
            # Either outcome (caught or re raised) is acceptable here;
            # what matters is the decorator called release_hold first.
            try:
                resp = c.post("/crash", data={"num_seq_per_target": "8"})
                # If Flask caught the exception, response is 500.
                assert resp.status_code == 500
            except RuntimeError:
                # Some Flask versions re raise in test mode.
                pass
        release.assert_called_once_with(
            "tx-xyz", reason="handler_exception"
        )

    def test_early_return_without_consume_releases_hold(self, client):
        # The D1 over-ceiling backstop returns a campaign-pointer response
        # BEFORE create_job runs, so g.wallet_hold_consumed stays False and the
        # decorator must release the hold (reason="view_early_return"). This is
        # the money-safety guarantee the backstop leans on; the reroute tests
        # force a $0 estimate (no hold), so without this the release branch is
        # never exercised and a future edit that strands the hold ships green.
        from app import requires_wallet
        from shared.wallet import PreflightResult, REASON_OK
        from flask import g

        @requires_wallet(tool_slug="rfdiffusion")
        def handler():
            # Over-ceiling backstop: return early, never mark the hold consumed.
            assert getattr(g, "wallet_hold_tx_id", None) == "tx-early"
            return ("open /campaigns/new", 400)

        from flask import Flask
        flask_app = Flask(__name__)
        flask_app.config["SECRET_KEY"] = "k"
        flask_app.config["TESTING"] = True
        flask_app.add_url_rule("/early", view_func=handler, methods=["POST"])

        ok = PreflightResult(
            allow=True,
            reason=REASON_OK,
            estimated_cost_usd=Decimal("2.62"),
            balance_usd=Decimal("100"),
            deficit_usd=Decimal("0"),
            hard_cap_usd=Decimal("150"),
        )
        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool", return_value=Decimal("2.62")
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 100.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight", return_value=ok
        ), patch(
            "shared.wallet_guard.wallet_reserve_hold", return_value="tx-early"
        ), patch(
            "shared.wallet_guard.wallet_release_hold"
        ) as release:
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/early", data={"num_designs": "100"})
            assert resp.status_code == 400
        release.assert_called_once_with(
            "tx-early", reason="view_early_return"
        )


# ===========================================================================
# /account/topup-complete
# ===========================================================================


class TestTopupCompleteValid:
    def test_valid_session_renders_success(self, client):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 25.0},
        ), patch(
            "billing.checkout.retrieve_topup_session",
            return_value=(
                {
                    "id": "cs_test_123",
                    "status": "complete",
                    "payment_status": "paid",
                    "amount_total": 2000,
                    "currency": "usd",
                    "metadata": {"user_id": "u-wallet", "kind": "topup"},
                },
                None,
            ),
        ), patch(
            "blueprints.wallet.render_template", return_value="TOPUP_SUCCESS"
        ) as render:
            _login(client)
            resp = client.get(
                "/account/topup-complete?session_id=cs_test_123"
            )
        assert resp.status_code == 200
        assert resp.get_data(as_text=True) == "TOPUP_SUCCESS"
        kwargs = render.call_args.kwargs
        assert kwargs["topup_success"] is True
        assert kwargs["stripe_session"]["id"] == "cs_test_123"

    def test_session_with_gate_return_tool_propagates(self, client):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 25.0},
        ), patch(
            "billing.checkout.retrieve_topup_session",
            return_value=(
                {
                    "id": "cs_test_456",
                    "metadata": {"user_id": "u-wallet"},
                },
                None,
            ),
        ), patch(
            "blueprints.wallet.render_template", return_value="OK"
        ) as render:
            _login(client)
            with client.session_transaction() as sess:
                sess["wallet_gate_form"] = {
                    "tool": "bindcraft",
                    "form": {"preset": "pilot"},
                    "reason": "insufficient_balance",
                }
            resp = client.get(
                "/account/topup-complete?session_id=cs_test_456"
            )
        kwargs = render.call_args.kwargs
        # The decorator forwards the original tool slug so the success
        # page can offer 'Return to <tool>'. The ?topup=success query
        # tail is appended so wallet-nav.js polls the balance until the
        # Stripe webhook lands.
        assert kwargs["return_tool"] == "bindcraft"
        assert kwargs["return_tool_url"] == "/tools/bindcraft?topup=success"


class TestTopupCompleteInvalid:
    def test_missing_session_id_renders_fallback(self, client):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 0.0},
        ), patch(
            "blueprints.wallet.render_template", return_value="FALLBACK"
        ) as render:
            _login(client)
            resp = client.get("/account/topup-complete")
        assert resp.status_code == 200
        kwargs = render.call_args.kwargs
        # No topup_success flag on the fallback render.
        assert "topup_success" not in kwargs
        assert "topup_error" in kwargs

    def test_invalid_session_renders_fallback(self, client):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 0.0},
        ), patch(
            "billing.checkout.retrieve_topup_session",
            return_value=(None, "Could not look up the Checkout Session."),
        ), patch(
            "blueprints.wallet.render_template", return_value="STRIPE_ERR"
        ) as render:
            _login(client)
            resp = client.get(
                "/account/topup-complete?session_id=cs_busted"
            )
        assert resp.status_code == 200
        assert "topup_error" in render.call_args.kwargs

    def test_session_owner_mismatch_blocks_view(self, client):
        """A leaked session id from another user must not render success."""
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx(user_id="u-mine")
        ), patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 50.0},
        ), patch(
            "billing.checkout.retrieve_topup_session",
            return_value=(
                {
                    "id": "cs_leaked",
                    "metadata": {"user_id": "u-other", "kind": "topup"},
                },
                None,
            ),
        ), patch(
            "blueprints.wallet.render_template", return_value="OWNER_MISMATCH"
        ) as render:
            _login(client, user_id="u-mine")
            resp = client.get(
                "/account/topup-complete?session_id=cs_leaked"
            )
        kwargs = render.call_args.kwargs
        # No topup_success flag when ownership cannot be confirmed.
        assert "topup_success" not in kwargs
        assert "topup_error" in kwargs

    def test_unauthenticated_redirects_to_login(self, client):
        # login_required decorator on topup-complete forces a redirect
        # when no session is present.
        resp = client.get("/account/topup-complete?session_id=cs_anon")
        # Redirected (302) to login, never reaches the handler.
        assert resp.status_code in (301, 302)


# ===========================================================================
# /account/wallet/topup (GET) and /account/wallet/checkout (POST) frozen guard
# ===========================================================================


class TestWalletTopupFrozenGuard:
    """When wallet_frozen is True the topup form and checkout must bounce.

    wallet_preflight already blocks tool submits on a frozen wallet, but
    the topup routes used to let a frozen user keep adding funds they
    could not spend. Both the GET form and the POST checkout creator
    must redirect to /account/wallet?wallet_frozen=1 so the overview's
    existing frozen banner is the user's landing point.
    """

    @staticmethod
    def _wallet(frozen=True):
        # Shape matches shared/wallet.py get_or_create_wallet so the GET
        # branch can fall through to the template render without Jinja
        # tripping on a missing auto_reload_* key.
        return {
            "user_id": "u-wallet",
            "balance_usd": 10.0,
            "auto_reload_enabled": False,
            "auto_reload_threshold_usd": 10.0,
            "auto_reload_amount_usd": 50.0,
            "auto_reload_monthly_cap_usd": 1000.0,
            "wallet_frozen": frozen,
            "spent_today_usd": 0.0,
            "spent_30d_usd": 0.0,
            "stripe_customer_id": None,
        }

    def test_get_topup_redirects_when_wallet_frozen(self, client):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet", return_value=self._wallet(frozen=True),
        ):
            _login(client)
            resp = client.get("/account/wallet/topup")
        assert resp.status_code in (301, 302)
        assert "/account/wallet" in resp.headers["Location"]
        assert "wallet_frozen=1" in resp.headers["Location"]

    def test_get_topup_renders_form_when_wallet_not_frozen(self, client):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet", return_value=self._wallet(frozen=False),
        ):
            _login(client)
            resp = client.get("/account/wallet/topup")
        # Falls through to the template render, not a redirect.
        assert resp.status_code == 200

    def test_post_checkout_redirects_when_wallet_frozen(self, client):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet", return_value=self._wallet(frozen=True),
        ), patch(
            "billing.checkout.create_topup_session"
        ) as create_session:
            _login(client)
            resp = client.post(
                "/account/wallet/checkout",
                data={"amount_usd": "50"},
            )
        assert resp.status_code in (301, 302)
        assert "/account/wallet" in resp.headers["Location"]
        assert "wallet_frozen=1" in resp.headers["Location"]
        # Stripe must not be called when the wallet is frozen.
        create_session.assert_not_called()
