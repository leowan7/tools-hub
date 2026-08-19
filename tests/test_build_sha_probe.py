"""The health probes report which commit is running.

Until 2026-08-19 neither /health nor /readyz said anything about the build, so
"the app is up" and "the app is up ON THE COMMIT I JUST MERGED" were the same
answer. Three separate ships had to be recorded as "on trunk, deploy
unconfirmed" for exactly this reason, and a session spent time trying and
failing to verify a deploy from outside.

These tests pin the contract that makes deploys verifiable:

  * the SHA comes from the environment, preferring Railway's injected variable
  * a missing variable reports "unknown" rather than lying or raising
  * ``status`` is UNCHANGED, because an external uptime monitor keyword-checks
    /readyz for "ready" (see the readyz docstring); adding a field must not
    disturb the string the monitor greps for

/readyz had no test coverage at all before this file.
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


@pytest.fixture(autouse=True)
def _no_ambient_build_sha(monkeypatch):
    """Neither variable may leak in from the developer's own environment."""
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_SHA", raising=False)


def test_health_reports_the_railway_injected_sha(client, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "b73bfadcafe1234567890abcdef")
    body = client.get("/health").get_json()
    assert body["build"] == "b73bfadcafe1234567890abcdef"


def test_health_falls_back_to_build_sha_for_non_railway_hosts(client, monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "deadbeef")
    assert client.get("/health").get_json()["build"] == "deadbeef"


def test_railway_wins_over_the_manual_override(client, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "from-railway")
    monkeypatch.setenv("BUILD_SHA", "stale-manual-value")
    assert client.get("/health").get_json()["build"] == "from-railway"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_variable_is_unknown_not_an_empty_string(client, monkeypatch, value):
    """A set-but-empty variable must not read as a successful answer."""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", value)
    assert client.get("/health").get_json()["build"] == "unknown"


def test_no_variable_at_all_reports_unknown(client):
    """Local runs have neither variable; the probe must still answer 200."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "build": "unknown"}


def test_readyz_carries_the_sha_on_the_happy_path(client, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123")
    fake = patch("shared.credits.get_service_client")
    with fake as get_client:
        get_client.return_value.table.return_value.select.return_value \
            .limit.return_value.execute.return_value = object()
        resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["build"] == "abc123"
    # The monitor greps this exact string. Adding a field must not move it.
    assert body["status"] == "ready"


def test_readyz_still_reports_the_sha_when_the_database_is_down(client, monkeypatch):
    """The degraded path is when you MOST need to know which commit is live."""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123")
    with patch("shared.credits.get_service_client", return_value=None):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["build"] == "abc123"
    assert body["status"] == "degraded"
    assert body["reason"] == "no_client"


def test_readyz_reports_the_sha_when_the_read_itself_raises(client, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123")
    with patch("shared.credits.get_service_client", side_effect=RuntimeError("boom")):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["build"] == "abc123"
    assert body["reason"] == "db_error"
