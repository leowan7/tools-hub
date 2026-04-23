"""Unit tests for the request idempotency middleware.

Stream G.1 (Wave-0 hardening). These tests use a fake Supabase client so
they run offline — no Railway / Supabase config required.

Usage
-----
    pytest tests/test_idempotency.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import patch

import pytest
from flask import Flask, jsonify

from shared.idempotency import _compute_key, idempotent


# ---------------------------------------------------------------------------
# Fake Supabase table + client
# ---------------------------------------------------------------------------


class _FakeTable:
    """Minimal in-memory stand-in for a Supabase table client."""

    def __init__(self, store: dict[str, dict]) -> None:
        self._store = store
        self._filter_key: Optional[str] = None
        self._update_payload: Optional[dict] = None
        self._pending_upsert: Optional[dict] = None

    def select(self, *_args: Any, **_kwargs: Any) -> "_FakeTable":
        return self

    def eq(self, column: str, value: Any) -> "_FakeTable":
        if column == "key":
            self._filter_key = value
        return self

    def upsert(self, payload: dict, on_conflict: str = "key") -> "_FakeTable":
        self._pending_upsert = payload
        return self

    def update(self, payload: dict) -> "_FakeTable":
        self._update_payload = payload
        return self

    def execute(self) -> Any:
        if self._pending_upsert is not None:
            row = dict(self._pending_upsert)
            self._store[row["key"]] = row
            self._pending_upsert = None
            return type("R", (), {"data": [row]})()
        if self._update_payload is not None and self._filter_key is not None:
            existing = self._store.get(self._filter_key)
            if existing:
                existing.update(self._update_payload)
            self._update_payload = None
            self._filter_key = None
            return type("R", (), {"data": []})()
        if self._filter_key is not None:
            row = self._store.get(self._filter_key)
            data = [row] if row else []
            self._filter_key = None
            return type("R", (), {"data": data})()
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, store: dict[str, dict]) -> None:
        self._store = store

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(self._store)


@pytest.fixture
def fake_store():
    return {}


@pytest.fixture
def fake_client(fake_store):
    return _FakeClient(fake_store)


@pytest.fixture
def app(fake_client):
    """Flask app with an @idempotent route and stubbed user context."""
    flask_app = Flask(__name__)

    call_counter = {"count": 0}

    @flask_app.route("/echo", methods=["POST"])
    @idempotent(ttl_seconds=60)
    def echo():
        call_counter["count"] += 1
        from flask import request

        return (
            jsonify(
                {
                    "call": call_counter["count"],
                    "body": request.get_data(as_text=True),
                }
            ),
            200,
        )

    flask_app.call_counter = call_counter  # type: ignore[attr-defined]
    return flask_app


@pytest.fixture
def user_ctx():
    class _Ctx:
        user_id = "00000000-0000-0000-0000-000000000001"
        email = "test@example.com"
        tier = "scout_pro"
        balance = 100

    return _Ctx()


@pytest.fixture(autouse=True)
def patch_deps(fake_client, user_ctx):
    with patch(
        "shared.idempotency.get_service_client", return_value=fake_client
    ), patch(
        "shared.idempotency.load_user_context", return_value=user_ctx
    ):
        yield


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_key_is_deterministic_for_same_body(app, user_ctx):
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key_a = _compute_key(user_ctx.user_id, "/echo", b"hello")
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key_b = _compute_key(user_ctx.user_id, "/echo", b"hello")
    assert key_a == key_b


def test_key_differs_for_different_body(app, user_ctx):
    with app.test_request_context("/echo", method="POST"):
        a = _compute_key(user_ctx.user_id, "/echo", b"one")
        b = _compute_key(user_ctx.user_id, "/echo", b"two")
    assert a != b


def test_key_differs_for_different_user(app):
    with app.test_request_context("/echo", method="POST"):
        a = _compute_key("user-a", "/echo", b"same")
        b = _compute_key("user-b", "/echo", b"same")
    assert a != b


def test_header_overrides_body_hash(app, user_ctx):
    with app.test_request_context(
        "/echo", method="POST", data=b"body-one",
        headers={"Idempotency-Key": "client-supplied"},
    ):
        with_header = _compute_key(user_ctx.user_id, "/echo", b"body-one")
    with app.test_request_context(
        "/echo", method="POST", data=b"body-two",
        headers={"Idempotency-Key": "client-supplied"},
    ):
        same_header_diff_body = _compute_key(
            user_ctx.user_id, "/echo", b"body-two"
        )
    # Same client-supplied header should dedup even across different bodies.
    assert with_header == same_header_diff_body


# ---------------------------------------------------------------------------
# End-to-end via Flask test client
# ---------------------------------------------------------------------------


def test_first_call_runs_handler_and_caches(app):
    client = app.test_client()
    r = client.post("/echo", data=b"hello")
    assert r.status_code == 200
    assert r.json["call"] == 1
    assert app.call_counter["count"] == 1


def test_replay_returns_cached_response_without_rerunning(app):
    client = app.test_client()
    r1 = client.post("/echo", data=b"hello")
    r2 = client.post("/echo", data=b"hello")
    assert r1.json == r2.json
    # Handler invoked only once.
    assert app.call_counter["count"] == 1
    assert r2.headers.get("Idempotent-Replay") == "true"


def test_different_body_is_not_deduped(app):
    client = app.test_client()
    r1 = client.post("/echo", data=b"hello")
    r2 = client.post("/echo", data=b"world")
    assert r1.json["call"] == 1
    assert r2.json["call"] == 2
    assert app.call_counter["count"] == 2


def test_in_flight_returns_409(app, fake_store, user_ctx):
    """A second request that finds a claimed but incomplete row must 409."""
    # Pre-seed a claimed-but-incomplete row for the key the next call would
    # compute, then fire the request.
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key = _compute_key(user_ctx.user_id, "/echo", b"hello")
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    fake_store[key] = {
        "key": key,
        "user_id": user_ctx.user_id,
        "route": "/echo",
        "response_status": None,
        "response_body": None,
        "content_type": None,
        "expires_at": future.isoformat(),
    }

    r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 409
    assert r.json["status"] == "in_progress"
    # Handler should not have run.
    assert app.call_counter["count"] == 0


def test_expired_row_does_not_block_new_request(app, fake_store, user_ctx):
    """A stale row past expires_at must not short-circuit a new request."""
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key = _compute_key(user_ctx.user_id, "/echo", b"hello")
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    fake_store[key] = {
        "key": key,
        "user_id": user_ctx.user_id,
        "route": "/echo",
        "response_status": 200,
        "response_body": '{"stale": true}',
        "content_type": "application/json",
        "expires_at": past.isoformat(),
    }

    r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 200
    assert r.json["call"] == 1  # Fresh handler invocation.


def test_fail_open_when_supabase_unavailable(app, user_ctx):
    """If get_service_client returns None, the handler still runs."""
    with patch("shared.idempotency.get_service_client", return_value=None):
        r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 200
    assert app.call_counter["count"] == 1
