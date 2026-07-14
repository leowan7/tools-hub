"""FIX M2 (cso audit 2026-06-17, second half): app-wide CSRF protection.

The session cookie authenticates the entire web UI. Before this fix, only
/account/api-keys/* carried a CSRF token; every other authenticated POST
(wallet, account, admin, jobs, tools, campaigns, workspaces) had none.

create_app() now installs a before_request guard that rejects any state-
changing request on a non-exempt main-app route unless it presents the
per-session token (form field ``_csrf`` or header ``X-CSRF-Token``).

    pytest tests/test_csrf_protection.py -v
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    # conftest disables CSRF process-wide for the suite; this suite is the
    # one place that must exercise it, so force enforcement back on.
    monkeypatch.setenv("CSRF_PROTECT", "1")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


_TOKEN = "test-csrf-token-abc123"


def _seed_session(client, *, with_token=True):
    with client.session_transaction() as sess:
        sess["user_email"] = "leowan7@gmail.com"
        sess["user_id"] = "u-test"
        if with_token:
            sess["_csrf_token"] = _TOKEN


# ---------------------------------------------------------------------------
# Enforcement: missing / wrong token is rejected on a representative route
# ---------------------------------------------------------------------------


def test_post_without_token_is_rejected(app):
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/lab-projects/submit", data={"source_job_id": "x"})
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


def test_post_with_wrong_token_is_rejected(app):
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(
        "/lab-projects/submit", data={"_csrf": "not-the-token", "source_job_id": "x"}
    )
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


def test_post_with_valid_form_token_passes_csrf(app):
    """A matching ``_csrf`` form field clears the CSRF gate. The downstream
    view may still redirect / 400, but it must NOT be the CSRF 403."""
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(
        "/lab-projects/submit", data={"_csrf": _TOKEN, "source_job_id": "x"}
    )
    assert resp.status_code != 403


def test_post_with_valid_header_token_passes_csrf(app):
    """AJAX callers present the token via X-CSRF-Token. Exercised against a
    real fetch-driven route (/jobs/<id>/cancel)."""
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(
        f"/jobs/{uuid.uuid4()}/cancel", headers={"X-CSRF-Token": _TOKEN}
    )
    assert resp.status_code != 403


def test_ajax_cancel_without_token_is_rejected(app):
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(f"/jobs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET is never gated; the token is delivered to the page
# ---------------------------------------------------------------------------


def test_get_is_not_blocked_and_renders_token(app):
    client = app.test_client()
    resp = client.get("/login")
    assert resp.status_code == 200
    # Hidden field injected by csrf_input() into every form ...
    assert b'name="_csrf"' in resp.data
    # ... and the meta tag csrf_meta_value() emits for fetch/XHR callers.
    assert b'name="csrf-token"' in resp.data


# ---------------------------------------------------------------------------
# Exempt surfaces: server-to-server + the anonymous beacon are NOT gated
# ---------------------------------------------------------------------------


def test_analytics_beacon_is_exempt(app):
    """/api/track is the unauthenticated sendBeacon endpoint — exempt, so a
    tokenless POST must succeed (204), not 403."""
    client = app.test_client()
    resp = client.post("/api/track", json={"event_type": "unit_test"})
    assert resp.status_code != 403
    assert resp.status_code == 204


def test_webhook_ingress_is_exempt(app):
    """Server-to-server webhook ingress has its own token/HMAC and must not
    be subject to the cookie-CSRF guard (would break Stripe/Modal)."""
    client = app.test_client()
    resp = client.post("/webhooks/heartbeat", json={"job_id": "nope"})
    # Whatever the handler decides (400/200/etc.), it must not be the CSRF 403.
    assert resp.status_code != 403


def test_preflight_is_exempt(app):
    """Side-effect-free validation endpoint is exempt (fetch sends fresh
    FormData with no token)."""
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post("/tools/mpnn/preflight", data={})
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Unmatched routes 404 (not 403) — the guard defers to Flask routing
# ---------------------------------------------------------------------------


def test_unmatched_post_route_404s_not_403(app):
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/this/route/does/not/exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Blueprint allowlist: a NON-allowlisted blueprint's POST stays CSRF-enforced
# (guards the app.py -> blueprints refactor from silently dropping CSRF)
# ---------------------------------------------------------------------------


def test_non_allowlisted_blueprint_post_is_enforced(app):
    """The CSRF exemption is an allowlist ({scout, platform_api}), not a blanket
    'any blueprint route is exempt' pass. When the cookie-authenticated web UI
    (login, wallet, tools, jobs, admin) moves into blueprints, those
    state-changing POSTs MUST stay CSRF-enforced. Register a fresh cookie-UI
    blueprint and confirm a tokenless POST is rejected."""
    from flask import Blueprint

    bp = Blueprint("dummy_ui", __name__)

    @bp.route("/dummy-ui/save", methods=["POST"])
    def _save():  # pragma: no cover - CSRF blocks it before the body runs
        return "saved", 200

    app.register_blueprint(bp)
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/dummy-ui/save", data={})
    assert resp.status_code == 403
    assert b"CSRF" in resp.data
