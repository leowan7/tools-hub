"""Security + functional coverage for the /account/api-keys surface after it
moved out of create_app() into tools/platform_api/account_bp.py (Commit 8 of
the blueprint refactor).

These routes previously had NO direct integration test. This suite locks in
the three invariants the move must preserve, run with the PROD posture
(ENABLE_PLATFORM_API=1 AND CSRF_PROTECT=1):

  1. Flag-gating: with ENABLE_PLATFORM_API off, the whole surface 404s.
  2. Auth: every /account/api-keys route requires login.
  3. CSRF: every state-changing POST is rejected without the per-session
     token, EVEN THOUGH the routes now live in a blueprint (the path-based
     exemption in app._csrf_request_is_exempt keeps the global guard from
     double-gating; _csrf_ok remains the real enforcement).

A passing create/revoke WITH a valid token also proves the global CSRF guard
correctly exempts the path — otherwise the guard would 403 the api-keys token
(which is not the global _csrf_token) before _csrf_ok ever ran.
"""

from __future__ import annotations

import types

import pytest

_TOKEN = "platform-api-csrf-token-xyz"


def _build_app(monkeypatch, *, flag: str):
    monkeypatch.setenv("ENABLE_PLATFORM_API", flag)
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    monkeypatch.setenv("CSRF_PROTECT", "1")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def app(monkeypatch):
    """Platform API enabled (prod posture)."""
    return _build_app(monkeypatch, flag="1")


@pytest.fixture
def app_flag_off(monkeypatch):
    return _build_app(monkeypatch, flag="0")


def _login(client, *, with_token=True):
    with client.session_transaction() as sess:
        sess["user_email"] = "leowan7@gmail.com"
        sess["user_id"] = "u-test"
        if with_token:
            sess["_platform_api_csrf"] = _TOKEN


def _patch_ctx(monkeypatch):
    monkeypatch.setattr(
        "tools.platform_api.account_bp.load_user_context",
        lambda: types.SimpleNamespace(user_id="u-test"),
    )


# ---------------------------------------------------------------------------
# 1. Flag-gating — the whole surface is invisible with the flag off
# ---------------------------------------------------------------------------


def test_flag_off_hides_entire_surface(app_flag_off):
    client = app_flag_off.test_client()
    _login(client)  # even an authenticated user must see 404
    assert client.get("/.well-known/ai-plugin.json").status_code == 404
    assert client.get("/account/api-keys").status_code == 404
    assert client.post("/account/api-keys/create", data={}).status_code == 404
    assert client.post("/account/api-keys/x/revoke", data={}).status_code == 404


# ---------------------------------------------------------------------------
# 2. Manifest is public JSON; the account surface requires login
# ---------------------------------------------------------------------------


def test_manifest_is_public_json(app):
    resp = app.test_client().get("/.well-known/ai-plugin.json")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["schema_version"] == "v1"
    assert resp.headers["Cache-Control"] == "public, max-age=300"


def test_keys_page_requires_login(app):
    resp = app.test_client().get("/account/api-keys")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_create_requires_login(app):
    resp = app.test_client().post("/account/api-keys/create", data={})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_revoke_requires_login(app):
    resp = app.test_client().post("/account/api-keys/abc/revoke", data={})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_rotate_requires_login(app):
    resp = app.test_client().post(
        "/account/api-keys/rotate-webhook-secret", data={}
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# 3. GET renders and delivers the token
# ---------------------------------------------------------------------------


def test_get_keys_page_renders_and_delivers_token(app, monkeypatch):
    _patch_ctx(monkeypatch)
    monkeypatch.setattr("tools.platform_api.account_bp.list_keys", lambda uid: [])
    monkeypatch.setattr(
        "shared.api_keys.get_webhook_secret_display", lambda user_id: None
    )
    client = app.test_client()
    _login(client, with_token=False)
    resp = client.get("/account/api-keys")
    assert resp.status_code == 200
    assert b'name="_csrf"' in resp.data
    # A per-session token was minted into the session.
    with client.session_transaction() as sess:
        assert sess.get("_platform_api_csrf")


# ---------------------------------------------------------------------------
# 4. CSRF enforcement on the POSTs (the security guarantee)
# ---------------------------------------------------------------------------


def test_create_without_token_is_rejected_400(app, monkeypatch):
    _patch_ctx(monkeypatch)
    called = {"mint": False}

    def _mint(**kw):
        called["mint"] = True
        return ("rk_live_x", "rk_live", "whsec")

    monkeypatch.setattr("tools.platform_api.account_bp.mint_token", _mint)
    monkeypatch.setattr("tools.platform_api.account_bp.list_keys", lambda uid: [])
    monkeypatch.setattr(
        "shared.api_keys.get_webhook_secret_display", lambda user_id: None
    )
    client = app.test_client()
    _login(client, with_token=True)
    resp = client.post("/account/api-keys/create", data={"label": "x"})
    assert resp.status_code == 400
    assert b"CSRF" in resp.data
    assert called["mint"] is False  # no key minted on a CSRF failure


def test_create_with_valid_token_mints(app, monkeypatch):
    _patch_ctx(monkeypatch)
    seen = {}

    def _mint(*, user_id, role, label):
        seen.update(user_id=user_id, role=role, label=label)
        return ("rk_live_PLAINTEXT_ONCE", "rk_live", "whsec_ONCE")

    monkeypatch.setattr("tools.platform_api.account_bp.mint_token", _mint)
    monkeypatch.setattr("tools.platform_api.account_bp.list_keys", lambda uid: [])
    monkeypatch.setattr(
        "shared.api_keys.get_webhook_secret_display", lambda user_id: None
    )
    client = app.test_client()
    _login(client, with_token=True)
    resp = client.post(
        "/account/api-keys/create",
        data={"_csrf": _TOKEN, "label": "prod key", "role": "member"},
    )
    # Passing the global CSRF guard (CSRF_PROTECT=1) with only the api-keys
    # token proves the path exemption still fires; _csrf_ok then admits it.
    assert resp.status_code == 200
    assert seen == {"user_id": "u-test", "role": "member", "label": "prod key"}
    assert b"rk_live_PLAINTEXT_ONCE" in resp.data  # revealed exactly once


def test_revoke_without_token_is_rejected_400(app, monkeypatch):
    _patch_ctx(monkeypatch)
    called = {"revoke": False}

    def _revoke(**kw):
        called["revoke"] = True

    monkeypatch.setattr("tools.platform_api.account_bp.revoke_key", _revoke)
    monkeypatch.setattr("tools.platform_api.account_bp.list_keys", lambda uid: [])
    monkeypatch.setattr(
        "shared.api_keys.get_webhook_secret_display", lambda user_id: None
    )
    client = app.test_client()
    _login(client, with_token=True)
    resp = client.post("/account/api-keys/key-123/revoke", data={})
    assert resp.status_code == 400
    assert b"CSRF" in resp.data
    assert called["revoke"] is False  # destructive op blocked without CSRF


def test_rotate_without_token_is_rejected_400(app, monkeypatch):
    _patch_ctx(monkeypatch)
    called = {"rotate": False}

    def _rotate(**kw):
        called["rotate"] = True
        return "whsec_NEW"

    monkeypatch.setattr("shared.api_keys.rotate_webhook_secret", _rotate)
    monkeypatch.setattr("tools.platform_api.account_bp.list_keys", lambda uid: [])
    monkeypatch.setattr(
        "shared.api_keys.get_webhook_secret_display", lambda user_id: None
    )
    client = app.test_client()
    _login(client, with_token=True)
    resp = client.post("/account/api-keys/rotate-webhook-secret", data={})
    assert resp.status_code == 400
    assert b"CSRF" in resp.data
    assert called["rotate"] is False  # secret rotation blocked without CSRF


def test_revoke_with_valid_token_revokes_and_redirects(app, monkeypatch):
    _patch_ctx(monkeypatch)
    seen = {}

    def _revoke(*, key_id, user_id):
        seen.update(key_id=key_id, user_id=user_id)

    monkeypatch.setattr("tools.platform_api.account_bp.revoke_key", _revoke)
    client = app.test_client()
    _login(client, with_token=True)
    resp = client.post("/account/api-keys/key-123/revoke", data={"_csrf": _TOKEN})
    assert resp.status_code == 302
    assert "/account/api-keys" in resp.headers["Location"]
    assert seen == {"key_id": "key-123", "user_id": "u-test"}
