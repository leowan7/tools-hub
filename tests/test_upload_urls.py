"""Tests for /api/upload-urls/<job_id>/<job_token> (Modal-facing).

Mirrors the test_cancel_race.py pattern: build a minimal Flask app and
register only the upload-URLs module so the test surface is the route
in isolation. ``get_job`` and ``presigned_output_put_url`` are patched
at the webhooks.uploads module namespace.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from flask import Flask

from webhooks import uploads as uploads_mod
from webhooks.uploads import MAX_FILENAMES_PER_REQUEST


def _job(**over):
    """Build a minimal ToolJob-shaped object the handler needs.

    The handler only touches ``job_token`` and ``user_id``/``id``, so a
    MagicMock with the right attrs is enough — no need for the full
    ToolJob model.
    """
    job = MagicMock()
    job.id = over.get("id", str(uuid.uuid4()))
    job.user_id = over.get("user_id", str(uuid.uuid4()))
    job.job_token = over.get("job_token", "good-token-12345")
    return job


@pytest.fixture
def client():
    app = Flask(__name__)
    uploads_mod.register_upload_urls(app)
    return app.test_client()


class TestAuth:
    """job_id lookup + job_token compare."""

    def test_unknown_job_returns_404(self, client, monkeypatch):
        monkeypatch.setattr(uploads_mod, "get_job", lambda _id: None)
        resp = client.post(
            "/api/upload-urls/missing-job/any-token",
            json={"filenames": ["design_1.pdb"]},
        )
        assert resp.status_code == 404

    def test_token_mismatch_returns_403(self, client, monkeypatch):
        job = _job(job_token="real-token")
        monkeypatch.setattr(uploads_mod, "get_job", lambda _id: job)
        resp = client.post(
            f"/api/upload-urls/{job.id}/wrong-token",
            json={"filenames": ["design_1.pdb"]},
        )
        assert resp.status_code == 403


class TestPayloadValidation:
    """filenames must be a non-empty list of strings under the cap."""

    def _good_job(self, monkeypatch):
        job = _job()
        monkeypatch.setattr(uploads_mod, "get_job", lambda _id: job)
        return job

    def test_missing_filenames_returns_400(self, client, monkeypatch):
        job = self._good_job(monkeypatch)
        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={},
        )
        assert resp.status_code == 400
        assert "filenames" in resp.get_json()["error"]

    def test_empty_filenames_returns_400(self, client, monkeypatch):
        job = self._good_job(monkeypatch)
        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={"filenames": []},
        )
        assert resp.status_code == 400

    def test_non_list_filenames_returns_400(self, client, monkeypatch):
        job = self._good_job(monkeypatch)
        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={"filenames": "design_1.pdb"},
        )
        assert resp.status_code == 400

    def test_too_many_filenames_returns_400(self, client, monkeypatch):
        job = self._good_job(monkeypatch)
        many = [f"design_{i}.pdb" for i in range(MAX_FILENAMES_PER_REQUEST + 1)]
        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={"filenames": many},
        )
        assert resp.status_code == 400
        assert "too many" in resp.get_json()["error"]

    def test_non_string_filename_returns_400(self, client, monkeypatch):
        job = self._good_job(monkeypatch)
        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={"filenames": ["design_1.pdb", 42]},
        )
        assert resp.status_code == 400

    def test_blank_string_filename_returns_400(self, client, monkeypatch):
        job = self._good_job(monkeypatch)
        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={"filenames": ["   "]},
        )
        assert resp.status_code == 400


class TestSuccess:
    """Happy path returns one signed URL per filename, in the same order."""

    def test_returns_url_per_filename(self, client, monkeypatch):
        job = _job()
        monkeypatch.setattr(uploads_mod, "get_job", lambda _id: job)

        calls: list[dict] = []

        def fake_mint(*, user_id, job_id, filename):
            calls.append({"user_id": user_id, "job_id": job_id, "filename": filename})
            return f"https://signed.example/{user_id}/{job_id}/designs/{filename}?token=abc"

        monkeypatch.setattr(uploads_mod, "presigned_output_put_url", fake_mint)

        names = ["design_1.pdb", "design_2.pdb", "design_3.pdb"]
        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={"filenames": names},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body["urls"].keys()) == set(names)
        for name in names:
            assert job.id in body["urls"][name]
            assert name in body["urls"][name]
        # Storage helper called once per filename, scoped to job's user.
        assert [c["filename"] for c in calls] == names
        assert all(c["user_id"] == job.user_id for c in calls)


class TestStorageFailure:
    """Storage minting failure surfaces as 502, not a crash."""

    def test_storage_error_returns_502(self, client, monkeypatch):
        from shared.storage import StorageError

        job = _job()
        monkeypatch.setattr(uploads_mod, "get_job", lambda _id: job)

        def boom(**_kw):
            raise StorageError("supabase unavailable")

        monkeypatch.setattr(uploads_mod, "presigned_output_put_url", boom)

        resp = client.post(
            f"/api/upload-urls/{job.id}/{job.job_token}",
            json={"filenames": ["design_1.pdb"]},
        )
        assert resp.status_code == 502
        assert resp.get_json()["error"] == "storage unavailable"
