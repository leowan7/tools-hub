"""First-run path: signup signs the browser in, the no-runs hero does not lead
with a top-up, the pay gate logs ``credit_exhausted``, and the purpose field's
placeholder is too short to pass as an answer."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from flask import render_template

from shared.auth import MIN_PURPOSE_CHARS, SignupResult

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def test_signup_signs_in_and_redirects_to_next(app):
    client = app.test_client()
    ok = SignupResult(success=True, user_id="user-123", classification="business")
    with patch("shared.auth.register_user", return_value=ok):
        resp = client.post("/signup", data={
            "email": "a@lab.example", "password": "longenough1",
            "password2": "longenough1", "terms_accepted": "on", "next": "/tools",
        })
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/tools"
    with client.session_transaction() as sess:
        assert sess["user_id"] == "user-123"
        assert sess["user_email"] == "a@lab.example"


def test_failed_signup_does_not_sign_in(app):
    client = app.test_client()
    bad = SignupResult(success=False, error_message="no", failure_code="purpose_missing")
    with patch("shared.auth.register_user", return_value=bad):
        resp = client.post("/signup", data={
            "email": "a@gmail.com", "password": "longenough1",
            "password2": "longenough1", "terms_accepted": "on",
        })
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert "user_id" not in sess and "user_email" not in sess


_WALLET = {
    "user_id": "user-123", "balance_usd": 0.0,
    "auto_reload_enabled": False, "auto_reload_threshold_usd": 10.0,
    "auto_reload_amount_usd": 50.0, "auto_reload_monthly_cap_usd": 1000.0,
    "wallet_frozen": False, "spent_today_usd": 0.0, "spent_30d_usd": 0.0,
    "stripe_customer_id": None,
}


def _hero(app, recent_jobs):
    with app.test_request_context("/"):
        html = render_template("index.html", tools=[], grouped=[],
                               recent_jobs=recent_jobs, authenticated=True)
    return html.split('class="landing-hero-cta"', 1)[1].split("</div>", 1)[0]


def test_hero_for_an_account_with_no_runs_does_not_lead_with_top_up(app):
    hero = _hero(app, [])
    assert "Top up" not in hero
    assert "Find the tool for your target" in hero and "/scout/" in hero


def test_hero_for_an_account_with_runs_keeps_top_up(app):
    job = {"id": "abcdef123456", "tool": "boltz2", "preset": "pilot", "status": "succeeded"}
    assert "Top up your wallet" in _hero(app, [job])


@pytest.mark.parametrize("reason, logged", [
    ("wallet_empty", True),
    ("insufficient_balance", True),
    ("wallet_frozen", False),
    ("per_tool_cap_exceeded", False),
])
def test_pay_gate_render_logs_credit_exhausted(app, reason, logged):
    from shared.wallet_guard import _render_topup_gate  # noqa: PLC0415

    with app.test_request_context("/tools/boltz2/submit"), patch(
        "shared.wallet_guard.get_or_create_wallet", return_value=_WALLET,
    ), patch("shared.events.log_event") as log_event:
        from flask import session
        session["user_id"] = "user-123"
        _render_topup_gate(tool_slug="boltz2", reason=reason, form_snapshot={})
    assert log_event.called == logged
    if logged:
        kwargs = log_event.call_args.kwargs
        assert kwargs["event_type"] == "credit_exhausted"
        assert kwargs["user_id"] == "user-123"
        assert kwargs["props"]["tool"] == "boltz2"


def test_purpose_placeholder_is_too_short_to_pass_and_browser_gets_the_rule(app):
    html = app.test_client().get("/signup").get_data(as_text=True)
    box = re.search(r'<textarea id="signup-purpose".*?>', html, re.S).group(0)
    placeholder = re.search(r'placeholder="([^"]*)"', box).group(1)
    assert 0 < len(placeholder) < MIN_PURPOSE_CHARS
    assert '"gmail.com"' in html and f"< {MIN_PURPOSE_CHARS};" in html
