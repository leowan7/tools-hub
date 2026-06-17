"""FIX M2 (cso audit 2026-06-17): session-cookie hardening is unconditional.

The Flask session cookie authenticates the ENTIRE web UI (wallet, account,
admin), not just the platform API. Before the fix, SESSION_COOKIE_SECURE /
HTTPONLY / SAMESITE were set only inside the ``ENABLE_PLATFORM_API`` block,
so with that flag off (the prod default) every authenticated POST surface
ran with Flask defaults — no Secure, SameSite unset.

These tests lock in:
  * HttpOnly + SameSite are set regardless of ENABLE_PLATFORM_API.
  * SameSite is "Lax" (not "Strict" — Strict drops the cookie on the
    post-login ``?next=`` cross-site top-level navigation).
  * Secure follows the HTTPS gate so local http dev login still works.

    pytest tests/test_cookie_config.py -v
"""

from __future__ import annotations

import pytest


def _make_app(monkeypatch, *, platform_api, public_base=None, railway=None):
    """Build a fresh app with a controlled, cookie-relevant environment.

    ``create_app`` reads the env at call time, so we set every input the
    cookie gate depends on and clear the ones we want absent — otherwise
    the test runner's ambient environment could leak in and flip Secure.
    """
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    # Keep the webhook-sweep background thread out of the test process.
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    monkeypatch.setenv("ENABLE_PLATFORM_API", "1" if platform_api else "0")

    if public_base is None:
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("PUBLIC_BASE_URL", public_base)
    if railway is None:
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", railway)

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


# ---------------------------------------------------------------------------
# HttpOnly + SameSite are set independent of the platform-api flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform_api", [False, True])
def test_httponly_set_regardless_of_platform_api(monkeypatch, platform_api):
    app = _make_app(monkeypatch, platform_api=platform_api)
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True


@pytest.mark.parametrize("platform_api", [False, True])
def test_samesite_is_lax_regardless_of_platform_api(monkeypatch, platform_api):
    app = _make_app(monkeypatch, platform_api=platform_api)
    # Lax, not Strict: Strict would drop the session cookie on the first
    # cross-site top-level navigation, breaking the post-login ?next= redirect.
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


# ---------------------------------------------------------------------------
# Secure follows the HTTPS / prod gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform_api", [False, True])
def test_secure_true_when_public_base_is_https(monkeypatch, platform_api):
    app = _make_app(
        monkeypatch,
        platform_api=platform_api,
        public_base="https://tools.ranomics.com",
    )
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_secure_true_when_railway_environment_present(monkeypatch):
    # Prod on Railway with no explicit https base still gets Secure on
    # (RAILWAY_ENVIRONMENT is injected in prod, absent locally).
    app = _make_app(
        monkeypatch, platform_api=False, public_base=None, railway="production"
    )
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_secure_false_for_local_http_dev(monkeypatch):
    # Explicit http base, not on Railway → local dev. Secure MUST be off or
    # the cookie never returns over http://127.0.0.1 and login silently fails.
    app = _make_app(
        monkeypatch, platform_api=False, public_base="http://localhost:5000"
    )
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_secure_false_when_no_https_signal_at_all(monkeypatch):
    # Neither a https base nor RAILWAY_ENVIRONMENT → treat as insecure/local.
    app = _make_app(
        monkeypatch, platform_api=False, public_base=None, railway=None
    )
    assert app.config["SESSION_COOKIE_SECURE"] is False
