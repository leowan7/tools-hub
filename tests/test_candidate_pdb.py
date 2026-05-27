"""Tests for /api/jobs/<job_id>/pdb/<filename> (browser-facing).

Drives the full Flask app via ``create_app()`` so the @login_required
decorator, ``load_user_context``, and the storage-vs-inline resolver
all execute. Storage helpers are patched at the ``app`` module
namespace (the ``from shared.storage import ...`` binding inside app.py)
so no real Supabase calls fire.

Login is faked by writing a context object to ``flask.session`` via a
shim — same trick the wallet API tests use.
"""

from __future__ import annotations

import base64
import uuid
from unittest.mock import MagicMock

import pytest

import app as app_mod


def _candidate(pdb_key: str, *, b64: str | None = None) -> dict:
    cand = {"pdb_key": pdb_key, "scores": {"ipTM": 0.5}}
    if b64 is not None:
        cand["pdb_content_b64"] = b64
    return cand


@pytest.fixture
def flask_app(monkeypatch):
    """Build the real app and short-circuit auth + Supabase init."""
    # Avoid touching real Supabase on app startup.
    monkeypatch.setattr(app_mod, "get_service_client", lambda: None, raising=False)
    application = app_mod.create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def _login(client, user_id: str):
    """Stash a session that passes shared.auth.login_required.

    The decorator checks ``session["user_email"]``; load_user_context
    is separately monkeypatched to return our fake context.
    """
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "test@example.com"
        sess["access_token"] = "fake-token"


def _patch_user_ctx(monkeypatch, user_id: str):
    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.email = "test@example.com"
    monkeypatch.setattr(app_mod, "load_user_context", lambda: ctx)


def _patch_job(monkeypatch, job):
    monkeypatch.setattr(app_mod, "get_job", lambda _id, user_id=None: job)


def _job(*, candidates=None, user_id=None):
    job = MagicMock()
    job.id = str(uuid.uuid4())
    job.user_id = user_id or str(uuid.uuid4())
    job.result = {"candidates": candidates or []}
    job.tool = "pxdesign"
    return job


class TestAuth:
    def test_no_session_redirects_to_login(self, client, monkeypatch):
        # load_user_context returns None when there's no session.
        monkeypatch.setattr(app_mod, "load_user_context", lambda: None)
        # The @login_required decorator itself redirects before our
        # route body runs; either way the user lands at /login.
        resp = client.get("/api/jobs/abc/pdb/design_1.pdb", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers["Location"]

    def test_owner_mismatch_returns_404(self, client, monkeypatch):
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)
        # get_job returns None when the row doesn't belong to the caller.
        monkeypatch.setattr(app_mod, "get_job", lambda _id, user_id=None: None)
        resp = client.get(
            "/api/jobs/some-job/pdb/design_1.pdb", follow_redirects=False
        )
        assert resp.status_code == 404


class TestStoragePath:
    """When the Storage object exists, bytes are proxied from download_output."""

    def test_storage_hit_returns_bytes(self, client, monkeypatch):
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)
        job = _job(user_id=user_id, candidates=[_candidate("design_1.pdb")])
        _patch_job(monkeypatch, job)

        monkeypatch.setattr(
            app_mod, "output_exists",
            lambda **_kw: True,
        )
        monkeypatch.setattr(
            app_mod, "download_output",
            lambda **_kw: b"ATOM      1  N   ALA A   1\n",
        )
        resp = client.get(f"/api/jobs/{job.id}/pdb/design_1.pdb")
        assert resp.status_code == 200
        assert resp.mimetype == "chemical/x-pdb"
        assert resp.data.startswith(b"ATOM")
        assert "design_1.pdb" in resp.headers["Content-Disposition"]


class TestInlineFallback:
    """When Storage misses, fall back to inline pdb_content_b64."""

    def test_inline_b64_path_returns_decoded_bytes(self, client, monkeypatch):
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)

        pdb_text = b"ATOM      1  N   ALA A   1\n"
        b64 = base64.b64encode(pdb_text).decode()
        job = _job(
            user_id=user_id,
            candidates=[_candidate("design_2.pdb", b64=b64)],
        )
        _patch_job(monkeypatch, job)

        monkeypatch.setattr(app_mod, "output_exists", lambda **_kw: False)

        resp = client.get(f"/api/jobs/{job.id}/pdb/design_2.pdb")
        assert resp.status_code == 200
        assert resp.data == pdb_text
        assert resp.mimetype == "chemical/x-pdb"

    def test_no_matching_candidate_returns_404(self, client, monkeypatch):
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)
        job = _job(
            user_id=user_id,
            candidates=[_candidate("design_other.pdb", b64="aGVsbG8=")],
        )
        _patch_job(monkeypatch, job)
        monkeypatch.setattr(app_mod, "output_exists", lambda **_kw: False)

        resp = client.get(f"/api/jobs/{job.id}/pdb/missing.pdb")
        assert resp.status_code == 404


class TestPdbKeyPrefix:
    """Pipelines emit pdb_key as either ``"design_0.pdb"`` or
    ``"designs/design_0.pdb"`` — both must route to the same Storage
    path and match inline-fallback rows on basename."""

    def test_inline_match_with_designs_prefix(self, client, monkeypatch):
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)

        pdb_text = b"ATOM      1  N   ALA A   1\n"
        b64 = base64.b64encode(pdb_text).decode()
        job = _job(
            user_id=user_id,
            candidates=[_candidate("designs/design_0.pdb", b64=b64)],
        )
        _patch_job(monkeypatch, job)
        monkeypatch.setattr(app_mod, "output_exists", lambda **_kw: False)

        # URL request preserves the same "designs/" prefix that the
        # template emits via {{ pdb_key | urlencode }}.
        resp = client.get(f"/api/jobs/{job.id}/pdb/designs/design_0.pdb")
        assert resp.status_code == 200
        assert resp.data == pdb_text

    def test_inline_match_when_request_is_basename_but_key_has_prefix(
        self, client, monkeypatch
    ):
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)

        pdb_text = b"ATOM      1  N   ALA A   1\n"
        b64 = base64.b64encode(pdb_text).decode()
        job = _job(
            user_id=user_id,
            candidates=[_candidate("designs/design_0.pdb", b64=b64)],
        )
        _patch_job(monkeypatch, job)
        monkeypatch.setattr(app_mod, "output_exists", lambda **_kw: False)

        # Hypothetical client that strips the prefix should still match.
        resp = client.get(f"/api/jobs/{job.id}/pdb/design_0.pdb")
        assert resp.status_code == 200
        assert resp.data == pdb_text


class TestStorageErrorFallthrough:
    """StorageError on the Storage path falls through to inline rather than 500."""

    def test_storage_error_falls_through_to_inline(self, client, monkeypatch):
        from shared.storage import StorageError

        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)

        pdb_text = b"ATOM      1  N   ALA A   1\n"
        b64 = base64.b64encode(pdb_text).decode()
        job = _job(
            user_id=user_id,
            candidates=[_candidate("design_3.pdb", b64=b64)],
        )
        _patch_job(monkeypatch, job)

        def boom(**_kw):
            raise StorageError("supabase 5xx")

        monkeypatch.setattr(app_mod, "output_exists", boom)
        # Inline path still works.
        resp = client.get(f"/api/jobs/{job.id}/pdb/design_3.pdb")
        assert resp.status_code == 200
        assert resp.data == pdb_text
