"""Open-redirect guard on the /login ``next`` parameter (cso audit L1).

A leading "/" is not a sufficient same-origin check: browsers treat
``//host`` (protocol-relative) and ``/\\host`` as absolute URLs to a
foreign origin. The login redirect must collapse those to "/".
"""

from __future__ import annotations

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


def _login(client, next_value):
    with patch("shared.auth.verify_login", return_value=(True, None, "user-123")):
        return client.post(
            "/login",
            data={"email": "a@b.com", "password": "x", "next": next_value},
        )


@pytest.mark.parametrize(
    "hostile",
    ["//evil.com", "/\\evil.com", "https://evil.com", "http://evil.com"],
)
def test_login_rejects_offsite_next(client, hostile):
    resp = _login(client, hostile)
    assert resp.status_code == 302
    # Never redirect to the foreign origin; collapse to root.
    assert resp.headers["Location"] in ("/", "http://localhost/")


@pytest.mark.parametrize("safe", ["/jobs", "/account/wallet", "/tools/mpnn"])
def test_login_preserves_safe_next(client, safe):
    resp = _login(client, safe)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(safe)
