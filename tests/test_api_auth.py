"""Unit tests for shared.api_auth — the Bearer-token decorator.

    pytest tests/test_api_auth.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask, g, jsonify

from shared import api_auth as api_auth_mod
from shared.api_keys import APIKeyContext


def _ctx(role: str = "member", revoked_at=None) -> APIKeyContext:
    return APIKeyContext(
        key_id="key-1",
        user_id="user-1",
        role=role,
        prefix="rk_live_abcd",
        label="t",
        created_at=None,
        last_used_at=None,
        revoked_at=revoked_at,
    )


def _build_app(read_only: bool = False) -> Flask:
    app = Flask(__name__)

    @app.route("/protected")
    @api_auth_mod.api_auth_required(read_only=read_only)
    def protected():
        return jsonify(
            {
                "user": g.api_user_id,
                "role": g.api_key_role,
                "key": g.api_key_id,
            }
        )

    return app


def test_missing_header_returns_401():
    app = _build_app()
    client = app.test_client()
    resp = client.get("/protected")
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["error"]["code"] == "missing_credentials"
    assert resp.headers["X-Robots-Tag"] == "noindex"


def test_malformed_header_returns_401():
    app = _build_app()
    client = app.test_client()
    resp = client.get("/protected", headers={"Authorization": "Token xyz"})
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "missing_credentials"


def test_invalid_token_returns_401():
    app = _build_app()
    client = app.test_client()
    with patch.object(api_auth_mod, "resolve_token", return_value=None):
        resp = client.get(
            "/protected",
            headers={"Authorization": "Bearer rk_live_bogus"},
        )
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "invalid_api_key"


def test_revoked_token_returns_401():
    app = _build_app()
    client = app.test_client()
    revoked = _ctx(revoked_at="2026-06-04T00:00:00+00:00")
    with patch.object(api_auth_mod, "resolve_token", return_value=revoked):
        resp = client.get(
            "/protected",
            headers={"Authorization": "Bearer rk_live_revoked"},
        )
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "invalid_api_key"


def test_viewer_on_write_returns_403():
    app = _build_app(read_only=False)  # write route
    client = app.test_client()
    viewer = _ctx(role="viewer")
    with patch.object(api_auth_mod, "resolve_token", return_value=viewer):
        resp = client.get(
            "/protected",
            headers={"Authorization": "Bearer rk_live_viewer"},
        )
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "forbidden_role"


def test_viewer_on_read_only_passes():
    app = _build_app(read_only=True)
    client = app.test_client()
    viewer = _ctx(role="viewer")
    with patch.object(api_auth_mod, "resolve_token", return_value=viewer):
        resp = client.get(
            "/protected",
            headers={"Authorization": "Bearer rk_live_viewer"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"] == "user-1"
    assert body["role"] == "viewer"


def test_member_on_write_populates_g():
    app = _build_app(read_only=False)
    client = app.test_client()
    member = _ctx(role="member")
    with patch.object(api_auth_mod, "resolve_token", return_value=member):
        resp = client.get(
            "/protected",
            headers={"Authorization": "Bearer rk_live_member"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"] == "user-1"
    assert body["role"] == "member"
    assert body["key"] == "key-1"
