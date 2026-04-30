"""Resource-not-found pages render the proper 404 template.

When a customer clicks a job-completion email link in a browser session
signed into the wrong account, ``get_job`` returns None for owner-scoped
lookups and the route used to render ``coming_soon.html`` ("We are
finalising this tool") — confusing because the *tool* is fine, the user
is just signed in as someone else.

This test locks the post-fix behaviour: resource-not-found paths render
``404.html`` (which explains the wrong-account possibility), with the
``coming_soon.html`` template reserved for the genuine "tool flag is
off / not yet released" case.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _login_session(client, email="leowan7@gmail.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def _ctx(email="leowan7@gmail.com"):
    return SimpleNamespace(
        user_id="u-different-from-job-owner",
        tier="free",
        balance=0,
        email=email,
    )


class TestJobNotFoundPage:
    """``/jobs/<uuid>`` renders 404.html when get_job returns None."""

    def test_returns_404_status(self, app, monkeypatch):
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())
        client = app.test_client()
        _login_session(client)
        with patch("app.get_job", return_value=None):
            resp = client.get(f"/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_body_says_not_found_not_coming_soon(self, app, monkeypatch):
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())
        client = app.test_client()
        _login_session(client)
        with patch("app.get_job", return_value=None):
            resp = client.get(f"/jobs/{uuid.uuid4()}")
        body = resp.get_data(as_text=True)
        assert "Not found" in body
        # The misleading "We are finalising this tool" copy must NOT appear
        assert "finalising this tool" not in body

    def test_body_explains_wrong_account_when_signed_in(self, app, monkeypatch):
        """Signed-in users get a hint that they may be in the wrong account."""
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())
        client = app.test_client()
        _login_session(client, email="leowan7@gmail.com")
        with patch("app.get_job", return_value=None):
            resp = client.get(f"/jobs/{uuid.uuid4()}")
        body = resp.get_data(as_text=True)
        assert "leowan7@gmail.com" in body
        assert "different account" in body or "wrong" in body.lower()


class TestUnknownToolSlugStillRendersBrandedNotFound:
    """``/tools/<unknown>`` uses _require_tool — should also be branded 404."""

    def test_unknown_slug_returns_404(self, app, monkeypatch):
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())
        client = app.test_client()
        _login_session(client)
        resp = client.get("/tools/no-such-tool-slug")
        assert resp.status_code == 404
        body = resp.get_data(as_text=True)
        # "Not found" page, not the "coming soon" template
        assert "finalising this tool" not in body


class TestComingSoonStillUsedForFlagOff:
    """Flag-off tools must still render the coming_soon.html copy.

    A tool that exists in the registry but has its FLAG_TOOL_X=off is a
    genuine "not yet released" case — the template is correct.
    """

    def test_flag_off_renders_coming_soon(self, app, monkeypatch):
        # AF2 exists in the registry; with no FLAG_TOOL_AF2 env var, the
        # tool_enabled gate fails — that's the coming-soon case.
        monkeypatch.delenv("FLAG_TOOL_AF2", raising=False)
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())
        client = app.test_client()
        _login_session(client)
        resp = client.get("/tools/af2")
        assert resp.status_code == 404
        body = resp.get_data(as_text=True)
        assert "finalising this tool" in body
