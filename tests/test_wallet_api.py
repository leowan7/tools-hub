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

pytestmark = pytest.mark.usefixtures("isolate_supabase")


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

    def test_short_balance_is_a_deficit_not_a_ceiling(self, client):
        """A $5 wallet on a binder form gets the top-up gate, not the ceiling.

        ``hard_block`` used to be ``balance < estimate``, and the partial
        hides its top-up gate whenever ``hard_block`` is true, so a user a few
        dollars short saw "Estimate exceeds the ceiling for a single job"
        with a disabled submit and no price to pay.
        """
        with patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 5.0, "wallet_frozen": False},
        ):
            _login(client)
            resp = client.get("/api/wallet/estimate?tool=bindcraft&num_designs=1")
        body = resp.get_json()
        assert Decimal(body["estimate_usd"]) > Decimal("5")
        assert body["exceeds_hard_cap"] is False
        assert body["hard_block"] is False
        # Measured against what submit reserves (the hold), not the price.
        assert Decimal(body["required_usd"]) >= Decimal(body["estimate_usd"])
        assert Decimal(body["deficit_usd"]) == Decimal(body["required_usd"]) - 5
        assert Decimal(body["rounded_topup_usd"]) >= Decimal("20")

    def test_balance_covering_price_but_not_hold_is_short(self, client):
        """Enough for the shown price, not the hold: the form must still gate.

        Submit reserves ``cushioned_hold_usd`` and reserve_hold refuses a
        balance below it, so a deficit of 0 here meant a green form and a
        refusal at submit time.
        """
        with patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 7.0, "wallet_frozen": False},
        ), patch(
            "blueprints.wallet.estimated_cost_for_tool", return_value=Decimal("6.00"),
        ), patch(
            "blueprints.wallet.cushioned_hold_usd", return_value=Decimal("8.00"),
        ):
            _login(client)
            body = client.get("/api/wallet/estimate?tool=bindcraft&num_designs=1").get_json()
        assert body["required_usd"] == "8.00"
        assert Decimal(body["deficit_usd"]) == Decimal("1.00")

    def test_uses_form_params_for_estimate_scaling(self, client):
        """A larger single-job count scales the estimate and the cap.

        Both counts stay at or under bindcraft's single-container ceiling
        (6, down from 16 now that a campaign chunk is sized to the 14400 s
        timeout its pipeline enforces): above it the endpoint prices the
        full-size run instead, which has no single-job cap
        (TestFullSizeEstimate).
        """
        with patch("blueprints.wallet.get_or_create_wallet", return_value=None):
            small = client.get(
                "/api/wallet/estimate?tool=bindcraft&num_designs=2"
            ).get_json()
            large = client.get(
                "/api/wallet/estimate?tool=bindcraft&num_designs=6"
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
    def test_a_count_no_run_can_start_gets_no_price(self, client):
        """A typo into the millions is past the single-job ceiling AND past
        anything the full-size planner accepts, so no run can start at it
        and the panel shows no price rather than a clamped one."""
        with patch("blueprints.wallet.get_or_create_wallet", return_value=None):
            resp = client.get(
                "/api/wallet/estimate",
                query_string={"tool": "bindcraft", "num_designs": 10_000_000},
            )
        body = resp.get_json()
        assert body["ok"] is True
        assert body["estimate_usd"] is None
        assert "cannot start" in body["no_estimate_reason"]


class TestFullSizeEstimate:
    """Over the single-container ceiling the form prices the full-size run,
    the same figure the result page's "Run more candidates" card shows
    (shared/scale_up.py::quote)."""

    @pytest.mark.parametrize("tool", ["rfdiffusion", "proteina"])
    def test_form_price_equals_the_result_page_price(self, client, monkeypatch, tool):
        from types import SimpleNamespace as NS
        from shared import compute_campaigns as cc
        from shared.scale_up import quote
        monkeypatch.setenv(f"FLAG_TOOL_{tool.upper()}", "on")
        job = NS(id="j", tool=tool, preset="pilot", status="succeeded",
                 created_at="2026-01-01T00:00:00+00:00",
                 inputs={"num_designs": 4}, result={"candidates": [{"rank": 1}]},
                 error=None, gpu_seconds_used=None)
        with patch("shared.wallet.get_or_create_wallet",
                   return_value={"balance_usd": 1000}),                 patch("shared.feature_flags.tool_enabled", return_value=True):
            q = quote("u-1", job)
        assert q.route == "split" and q.count == 100
        with patch("blueprints.wallet.get_or_create_wallet") as gow:
            body = client.get(
                "/api/wallet/estimate",
                query_string={"tool": tool, "num_designs": 100, "preset": "pilot"},
            ).get_json()
        gow.assert_not_called()
        assert body["full_size_run"] is True
        assert Decimal(body["estimate_usd"]) == q.price_usd
        assert body["total_subjobs"] == cc.plan_chunks(tool, 100, "pilot").total_subjobs
        # Anonymous: no wallet-derived fields, no blocks.
        assert body["balance_usd"] is None and body["authenticated"] is False
        assert body["hard_block"] is False and body["soft_block"] is False

    def test_signed_in_deficit_is_against_the_start_gate(self, client):
        from shared import compute_campaigns as cc
        with patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value={"balance_usd": 1.0, "wallet_frozen": False},
        ):
            _login(client)
            body = client.get(
                "/api/wallet/estimate?tool=bindcraft&num_designs=100"
            ).get_json()
        plan = cc.plan_chunks("bindcraft", 100, "pilot")
        required = max(plan.budget_usd,
                       cc.first_wave_hold_usd(plan, cc.launch_concurrency_for("bindcraft")))
        assert Decimal(body["required_usd"]) == required
        assert Decimal(body["deficit_usd"]) == required - 1


class TestInvalidCountGetsNoPrice:
    @pytest.mark.parametrize("value", ["0", "-5", "abc", "2.5"])
    def test_invalid_count(self, client, value):
        with patch("blueprints.wallet.get_or_create_wallet", return_value=None):
            body = client.get(
                "/api/wallet/estimate",
                query_string={"tool": "bindcraft", "num_designs": value},
            ).get_json()
        assert body["ok"] is True
        assert body["estimate_usd"] is None
        assert body["no_estimate_reason"]


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
    """A zero estimate (a free tier) runs with no hold -- but is still gated.

    It used to return before ``wallet_preflight``, and this test asserted
    that. The one real free tier -- proteina's ``validate`` -- still spawns
    the A100 container its Modal app declares unconditionally, so the zero
    estimate now skips only the reservation: preflight runs, and a frozen
    wallet is refused like any other submit (see shared/wallet_guard.py's
    ``free_run``). The refusal itself is pinned against the REAL preflight in
    tests/test_free_presets_cost_nothing.py; preflight is a stub here.
    """

    def test_zero_estimate_is_preflighted_but_places_no_hold(self, client):
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

        from shared.wallet import PreflightResult, REASON_OK

        with flask_app.test_client() as c, patch(
            "shared.wallet_guard.load_user_context", return_value=_ctx()
        ), patch(
            "shared.wallet_guard.estimated_cost_for_tool",
            return_value=Decimal("0"),
        ), patch(
            "shared.wallet_guard.get_or_create_wallet",
            return_value={"balance_usd": 0.0, "wallet_frozen": False},
        ), patch(
            "shared.wallet_guard.wallet_preflight"
        ) as preflight, patch(
            "shared.wallet_guard.wallet_reserve_hold"
        ) as reserve:
            preflight.return_value = PreflightResult(
                allow=True,
                reason=REASON_OK,
                estimated_cost_usd=Decimal("0"),
                balance_usd=Decimal("0"),
                deficit_usd=Decimal("0"),
                hard_cap_usd=Decimal("0.15"),
            )
            with c.session_transaction() as sess:
                sess["user_id"] = "u-1"
            resp = c.post("/smoke", data={"preset": "smoke"})
        assert ran["called"] is True
        # Preflight IS consulted; only the reservation is skipped. This fails
        # if ``free_run`` moves back above the preflight call. What preflight
        # then DOES with a zero estimate is not exercised here -- it is a stub.
        preflight.assert_called_once()
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


def test_guard_decides_a_short_gate_on_the_hold():
    """Short on the price re-preflights on the hold, and the gate takes that reason."""
    from flask import Flask

    from app import requires_wallet
    from shared.wallet import REASON_INSUFFICIENT, PreflightResult

    @requires_wallet(tool_slug="bindcraft")
    def handler():  # pragma: no cover
        raise AssertionError("handler should not run when blocked")

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "k"
    flask_app.add_url_rule("/blocked", view_func=handler, methods=["POST"])
    flask_app.add_url_rule(
        "/tools/<tool>", endpoint="tools.tool_form", view_func=lambda tool: "form"
    )

    seen = []

    def fake_preflight(_uid, _slug, amount, _params):
        seen.append(amount)
        return PreflightResult(
            allow=False, reason=REASON_INSUFFICIENT, estimated_cost_usd=amount,
            balance_usd=Decimal("4.99"), deficit_usd=amount - Decimal("4.99"),
            hard_cap_usd=Decimal("8.00"),
        )

    with flask_app.test_client() as c, patch(
        "shared.wallet_guard.load_user_context", return_value=_ctx()
    ), patch(
        "shared.wallet_guard.estimated_cost_for_tool", return_value=Decimal("6.00"),
    ), patch(
        "shared.wallet_guard.cushioned_hold_usd", return_value=Decimal("8.00"),
    ), patch(
        "shared.wallet_guard.get_or_create_wallet", return_value={"balance_usd": 4.99},
    ), patch(
        "shared.wallet_guard.wallet_preflight", side_effect=fake_preflight,
    ), patch(
        "shared.wallet_guard.wallet_reserve_hold"
    ) as reserve, patch(
        "shared.wallet_guard.render_template", return_value=""
    ) as render:
        with c.session_transaction() as sess:
            sess["user_id"] = "u-1"
        c.post("/blocked", data={"num_designs": "1"})
    reserve.assert_not_called()
    assert seen == [Decimal("6.00"), Decimal("8.00")]
    assert render.call_args.kwargs["gate_reason"] == REASON_INSUFFICIENT


class TestTopupFromFormGate:
    """The form's gate links here with ``tool``."""

    def _get(self, client, qs):
        with patch(
            "blueprints.wallet.load_user_context", return_value=_ctx()
        ), patch(
            "blueprints.wallet.get_or_create_wallet",
            return_value=TestWalletTopupFrozenGuard._wallet(frozen=False),
        ):
            _login(client)
            return client.get("/account/wallet/topup" + qs)

    def test_tool_sets_the_return_without_a_notice(self, client):
        # No estimate on this route, so no claim the balance falls short: a
        # reload after a top-up would make it false.
        resp = self._get(client, "?tool=bindcraft&need=33.01")
        html = resp.get_data(as_text=True)
        assert 'value="20"' in html  # the minimum; a stale need= is ignored
        assert "wallet-topup-gate-notice" not in html
        assert "33.01" not in html and 'value="34"' not in html
        with client.session_transaction() as sess:
            assert sess["wallet_gate_form"] == {"tool": "bindcraft"}

    def test_junk_params_are_ignored(self, client):
        resp = self._get(client, "?tool=../evil&need=abc")
        assert resp.status_code == 200
        assert 'value="20"' in resp.get_data(as_text=True)
        with client.session_transaction() as sess:
            assert "wallet_gate_form" not in sess
