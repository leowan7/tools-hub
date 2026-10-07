"""Continue with Google: /auth/google and /auth/google/callback.

Google's token endpoint, the Supabase service client and the auth-user
lookup / creation helpers are all mocked; nothing leaves the process.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from shared.auth import SignupResult, find_auth_user_by_email

pytestmark = pytest.mark.usefixtures("isolate_supabase")

CLIENT_ID = "test-client.apps.googleusercontent.com"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.example.test")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _id_token(**overrides) -> str:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "email": "Ada@Example.org",
        "email_verified": True,
    }
    claims.update(overrides)
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{body}.sig"


def _start(client, next_value=None) -> str:
    """Hit /auth/google and return the state it sent to Google."""
    query = {"next": next_value} if next_value else {}
    resp = client.get("/auth/google", query_string=query)
    assert resp.status_code == 302
    target = urlsplit(resp.headers["Location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    params = parse_qs(target.query)
    assert params["redirect_uri"] == ["https://tools.example.test/auth/google/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid email profile"]
    return params["state"][0]


def _callback(client, state, *, id_token=None, user=None, register=None):
    token_resp = MagicMock()
    token_resp.json.return_value = {"id_token": id_token or _id_token()}
    with patch("blueprints.auth.requests.post", return_value=token_resp) as post, \
            patch("shared.credits.get_service_client", return_value=object()), \
            patch("shared.auth.find_auth_user_by_email", return_value=user), \
            patch("shared.auth.register_google_user", return_value=register) as reg, \
            patch("shared.events.log_event") as log_event, \
            patch("shared.events.emit") as emit:
        resp = client.get("/auth/google/callback", query_string={"code": "abc", "state": state})
    return resp, SimpleNamespace(post=post, register=reg, log_event=log_event, emit=emit)


def _event_types(mocks) -> list[str]:
    return [c.kwargs["event_type"] for c in mocks.log_event.call_args_list]


def test_new_user_is_created_and_signed_in(client):
    state = _start(client)
    created = SignupResult(
        success=True, user_id="new-1", classification="personal", signup_quality="personal"
    )
    resp, mocks = _callback(client, state, register=created)

    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    mocks.register.assert_called_once()
    assert mocks.register.call_args.args == ("ada@example.org",)
    # The PKCE verifier from the start step went to Google's token endpoint.
    assert mocks.post.call_args.kwargs["data"]["code_verifier"]
    assert _event_types(mocks) == ["signup_completed", "login"]
    assert mocks.log_event.call_args_list[0].kwargs["props"]["method"] == "google"
    assert mocks.emit.call_args.kwargs["properties"]["method"] == "google"
    with client.session_transaction() as sess:
        assert sess["user_id"] == "new-1"
        assert sess["user_email"] == "ada@example.org"
        assert "google_oauth" not in sess


def test_existing_user_is_signed_in(client):
    state = _start(client)
    resp, mocks = _callback(client, state, user={"id": "old-1", "email": "ada@example.org"})

    assert resp.status_code == 302
    mocks.register.assert_not_called()
    assert _event_types(mocks) == ["login"]
    assert mocks.log_event.call_args.kwargs["props"] == {"method": "google"}
    with client.session_transaction() as sess:
        assert sess["user_id"] == "old-1"


def test_banned_user_is_refused(client):
    state = _start(client)
    resp, _ = _callback(
        client, state,
        user={"id": "old-1", "email": "ada@example.org", "banned_until": "2999-01-01T00:00:00Z"},
    )
    assert resp.status_code == 200 and b"can&#39;t sign in" in resp.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_unverified_email_is_refused(client):
    state = _start(client)
    resp, mocks = _callback(client, state, id_token=_id_token(email_verified=False))

    assert resp.status_code == 200 and b"has not verified" in resp.data
    mocks.register.assert_not_called()
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_state_mismatch_is_refused(client):
    _start(client)
    resp, mocks = _callback(client, "forged", user={"id": "old-1", "email": "ada@example.org"})

    assert resp.status_code == 200 and b"did not complete" in resp.data
    mocks.post.assert_not_called()
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_next_survives_the_round_trip(client):
    state = _start(client, next_value="/tools/bindcraft?x=1")
    resp, _ = _callback(client, state, user={"id": "old-1", "email": "ada@example.org"})
    assert resp.headers["Location"] == "/tools/bindcraft?x=1"


def test_offsite_next_falls_back(client):
    state = _start(client, next_value="//evil.example")
    resp, _ = _callback(client, state, user={"id": "old-1", "email": "ada@example.org"})
    assert resp.headers["Location"] == "/"


def test_button_shows_on_both_tabs_when_configured(client):
    page = client.get("/login").data
    assert page.count(b"Continue with Google") == 2


def test_hidden_and_404_without_env(monkeypatch, client):
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    assert b"Continue with Google" not in client.get("/login").data
    assert b"Continue with Google" not in client.get("/signup").data
    assert client.get("/auth/google").status_code == 404
    assert client.get("/auth/google/callback").status_code == 404


def test_lookup_matches_exactly_and_pages():
    """filter= is a substring match server-side; only the exact address counts."""
    pages = [
        [{"id": f"u{i}", "email": f"xada@example.org{i}"} for i in range(1000)],
        [{"id": "hit", "email": "Ada@Example.org"}],
    ]
    calls = []

    def _request(method, path, query):
        calls.append(dict(query))
        resp = MagicMock()
        resp.json.return_value = {"users": pages[len(calls) - 1]}
        return resp

    fake = SimpleNamespace(auth=SimpleNamespace(admin=SimpleNamespace(_request=_request)))
    assert find_auth_user_by_email(fake, " ADA@example.org ")["id"] == "hit"
    assert [c["page"] for c in calls] == ["1", "2"]
    assert calls[0]["filter"] == "ada@example.org"

    calls.clear()
    pages[:] = [[{"id": "x", "email": "bada@example.org"}]]
    assert find_auth_user_by_email(fake, "ada@example.org") is None
    assert len(calls) == 1
