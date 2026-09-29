"""Every Platform API route, and what a read-only key may call.

Builds the real app (ENABLE_PLATFORM_API=1), enumerates its URL map, and
calls every /api/v1 route with a read-only key and with a full key. A
read-only key must get 403 ``forbidden_role`` on every route that is not in
READ_ONLY_ALLOWED, and must get past auth on every route that is. Adding a
route to the API without adding it to one of the three sets below fails
``test_every_route_is_classified``.

    pytest tests/test_api_key_scope_routes.py -v
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from shared.api_keys import APIKeyContext

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# (method, rule) a read-only key may call.
READ_ONLY_ALLOWED = {
    ("GET", "/api/v1/targets"),
    ("POST", "/api/v1/experiments/cost-estimate"),
    ("GET", "/api/v1/experiments/<experiment_id>"),
    ("GET", "/api/v1/experiments/<experiment_id>/quote"),
    ("GET", "/api/v1/experiments/<experiment_id>/results"),
}
# (method, rule) a read-only key must be refused on: submit, confirm (commits
# to a price), withdraw.
FULL_SCOPE_ONLY = {
    ("POST", "/api/v1/experiments"),
    ("DELETE", "/api/v1/experiments/<experiment_id>"),
    ("POST", "/api/v1/quotes/<quote_id>/confirm"),
}
# (method, rule) served without any key.
PUBLIC = {
    ("GET", "/api/v1/openapi.json"),
    ("GET", "/api/v1/docs"),
}


def _ctx(role: str) -> APIKeyContext:
    return APIKeyContext(
        key_id="k1",
        user_id="u1",
        role=role,
        prefix="rk_live_",
        label="t",
        created_at=None,
        last_used_at=None,
        revoked_at=None,
    )


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ENABLE_PLATFORM_API", "1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _api_routes(app) -> set[tuple[str, str]]:
    """Every (method, rule) under /api/v1, plus every Bearer-authenticated
    endpoint anywhere in the app. OPTIONS/HEAD are CORS/Flask plumbing."""
    found = set()
    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        if not (rule.rule.startswith("/api/v1") or hasattr(view, "api_read_only")):
            continue
        for method in rule.methods - {"OPTIONS", "HEAD"}:
            found.add((method, rule.rule))
    return found


def _url(rule: str) -> str:
    return re.sub(r"<[^>]+>", "exp-does-not-exist", rule)


def _call(client, method: str, rule: str, role: str):
    with patch("shared.api_auth.resolve_token", return_value=_ctx(role)):
        return client.open(
            _url(rule),
            method=method,
            json={},
            headers={"Authorization": "Bearer rk_live_scopetest"},
        )


def test_every_route_is_classified(app):
    assert _api_routes(app) == READ_ONLY_ALLOWED | FULL_SCOPE_ONLY | PUBLIC


def test_decorator_flag_matches_classification(app):
    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        for method in rule.methods - {"OPTIONS", "HEAD"}:
            key = (method, rule.rule)
            if key in PUBLIC:
                assert not hasattr(view, "api_read_only"), key
            elif key in READ_ONLY_ALLOWED:
                assert view.api_read_only is True, key
            elif key in FULL_SCOPE_ONLY:
                assert view.api_read_only is False, key


def test_openapi_documents_403_on_exactly_the_full_scope_routes():
    from tools.platform_api.openapi_spec import build_spec

    documented = {
        (method.upper(), "/api/v1" + re.sub(r"\{([^}]+)\}", r"<\1>", path))
        for path, ops in build_spec()["paths"].items()
        for method, op in ops.items()
        if "403" in op.get("responses", {})
    }
    assert documented == FULL_SCOPE_ONLY


@pytest.mark.parametrize("method,rule", sorted(FULL_SCOPE_ONLY))
def test_read_only_key_refused(app, method, rule):
    resp = _call(app.test_client(), method, rule, "viewer")
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"]["code"] == "forbidden_role"
    assert "read-only" in body["error"]["message"]


@pytest.mark.parametrize("method,rule", sorted(READ_ONLY_ALLOWED))
def test_read_only_key_allowed(app, method, rule):
    resp = _call(app.test_client(), method, rule, "viewer")
    assert resp.status_code not in (401, 403), resp.get_json()


@pytest.mark.parametrize("method,rule", sorted(READ_ONLY_ALLOWED | FULL_SCOPE_ONLY))
def test_full_key_passes_auth_everywhere(app, method, rule):
    resp = _call(app.test_client(), method, rule, "member")
    assert resp.status_code not in (401, 403), resp.get_json()


@pytest.mark.parametrize("method,rule", sorted(READ_ONLY_ALLOWED | FULL_SCOPE_ONLY))
def test_expired_key_refused_like_revoked(app, method, rule):
    expired = APIKeyContext(
        **{**_ctx("member").__dict__, "expires_at": "2020-01-01T00:00:00+00:00"}
    )
    with patch("shared.api_auth.resolve_token", return_value=expired):
        resp = app.test_client().open(
            _url(rule),
            method=method,
            json={},
            headers={"Authorization": "Bearer rk_live_scopetest"},
        )
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "invalid_api_key"


@pytest.mark.parametrize(
    "path",
    [
        "/account/api-keys/create",
        "/account/api-keys/rotate-webhook-secret",
        "/account/api-keys/some-key/revoke",
    ],
)
def test_key_and_webhook_management_refuses_bearer(app, path, monkeypatch):
    """Key and webhook-secret management is session-cookie only: a Bearer
    key of any scope is sent to login and nothing is minted or changed."""
    touched = []
    for name in ("mint_token", "revoke_key"):
        monkeypatch.setattr(
            f"tools.platform_api.account_bp.{name}",
            lambda **kw: touched.append(kw),
        )
    monkeypatch.setattr(
        "shared.api_keys.rotate_webhook_secret", lambda **kw: touched.append(kw)
    )
    for role in ("viewer", "member"):
        resp = _call(app.test_client(), "POST", path, role)
        assert resp.status_code in (302, 303), resp.status_code
        assert "/login" in resp.headers["Location"]
    assert touched == []
