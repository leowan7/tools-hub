"""Tests for /api/jobs/<job_id>/pdb/<filename> (browser-facing).

Drives the full Flask app via ``create_app()`` so the @login_required
decorator and the storage-vs-inline resolver both execute. Storage
helpers are patched on ``blueprints.jobs`` — the binding the route
body reads — rather than on ``app``.

Those patches do not keep Supabase out of the picture: rendering
404.html runs app.py's ``inject_workspace_context``, which reaches
``shared.credits.load_user_context`` through app.py's own import, a
binding this file never patches. The module-level ``isolate_supabase``
mark below is what blanks the credentials.

Login is faked by writing a context object to ``flask.session`` via a
shim — same trick the wallet API tests use.
"""

from __future__ import annotations

import base64
import io
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest

import app as app_mod
import blueprints.jobs as jobs_mod

# This file drives create_app() and real routes; without this the repo-root
# .env service-role credentials make those runs hit the live database.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _candidate(pdb_key: str, *, b64: str | None = None) -> dict:
    cand = {"pdb_key": pdb_key, "scores": {"ipTM": 0.5}}
    if b64 is not None:
        cand["pdb_content_b64"] = b64
    return cand


@pytest.fixture
def flask_app():
    """Build the real app."""
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
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: ctx)


def _patch_job(monkeypatch, job):
    monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)


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
        monkeypatch.setattr(jobs_mod, "load_user_context", lambda: None)
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
        monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: None)
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
            jobs_mod, "output_exists",
            lambda **_kw: True,
        )
        monkeypatch.setattr(
            jobs_mod, "download_output",
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

        monkeypatch.setattr(jobs_mod, "output_exists", lambda **_kw: False)

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
        monkeypatch.setattr(jobs_mod, "output_exists", lambda **_kw: False)

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
        monkeypatch.setattr(jobs_mod, "output_exists", lambda **_kw: False)

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
        monkeypatch.setattr(jobs_mod, "output_exists", lambda **_kw: False)

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

        monkeypatch.setattr(jobs_mod, "output_exists", boom)
        # Inline path still works.
        resp = client.get(f"/api/jobs/{job.id}/pdb/design_3.pdb")
        assert resp.status_code == 200
        assert resp.data == pdb_text


class TestZipFilename:
    """The archive name must not promise a format it may not carry.

    shared/exports.py writes each entry at its own pdb_key, so a
    boltzgen archive really does contain .cif members and the old
    job_<id>_pdbs.zip was false. A review found that reverting this
    route's name alone left the WHOLE suite green -- only the targets
    arm of the same rename was pinned.
    """

    def test_the_job_archive_is_named_structures_not_pdbs(
        self, client, monkeypatch,
    ):
        job = _job(candidates=[
            _candidate("designs/design_002.cif",
                       b64=base64.b64encode(b"data_x").decode()),
        ])
        _patch_user_ctx(monkeypatch, job.user_id)
        _patch_job(monkeypatch, job)
        _login(client, job.user_id)
        resp = client.get(f"/jobs/{job.id}/export.zip")
        assert resp.status_code == 200
        disposition = resp.headers["Content-Disposition"]
        assert f"job_{job.id[:8]}_structures.zip" in disposition
        assert "_pdbs" not in disposition
        # And the archive really does carry the .cif the name allows.
        names = zipfile.ZipFile(io.BytesIO(resp.get_data())).namelist()
        assert any(n.endswith(".cif") for n in names), names

class TestPdbKeyNotAString:
    """A container-written pdb_key whose type is not str.

    job.result is stored as the container sent it
    (webhooks/modal.py::_handle_result, blueprints/jobs.py::job_status), and
    shared/jobs.py::_slim_result_for_persist type-checks pdb_key rather than
    coercing it, so a non-str value arrives intact -- and posixpath.basename
    raises TypeError on an int, float, bool, dict or list.

    The blast radius is POSITION-dependent: this loop returns as soon as a
    row's basename matches, so a bad row only breaks the designs listed
    AFTER it. A test that puts the bad row last passes against the unfixed
    route and proves nothing -- hence the bad row at index 0 here, and the
    assertion on the HEALTHY design's bytes. The results page renders a
    download link for every row (candidate_table.html coerces pdb_key at
    its own definition, so the page survives to render them), which is what
    makes this reachable: working-looking buttons that 500.
    """

    @pytest.mark.parametrize("bad_key", [12345, 3.5, True, {"a": 1}, ["x"]])
    def test_bad_row_does_not_poison_a_later_design(
        self, client, monkeypatch, bad_key
    ):
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)

        pdb_text = b"ATOM      1  N   ALA A   1\n"
        b64 = base64.b64encode(pdb_text).decode()
        job = _job(
            user_id=user_id,
            candidates=[
                {"pdb_key": bad_key, "pdb_content_b64": b64},
                _candidate("design_2.pdb", b64=b64),
            ],
        )
        _patch_job(monkeypatch, job)
        # Inline fallback is the only path that reads pdb_key; Storage
        # hits return before the loop.
        monkeypatch.setattr(jobs_mod, "output_exists", lambda **_kw: False)

        resp = client.get(f"/api/jobs/{job.id}/pdb/design_2.pdb")
        assert resp.status_code == 200, (
            f"a row carrying pdb_key={bad_key!r} broke the download of a "
            f"later, healthy design (got {resp.status_code})"
        )
        assert resp.data == pdb_text

    def test_falsy_pdb_key_stays_falsy(self, client, monkeypatch):
        """A falsy pdb_key means "no structure reference", so it must not
        become a basename. Coercing with a bare str() would turn 0 into the
        truthy "0", and a request for /pdb/0 would then match that row and
        serve its bytes under a name it never claimed."""
        user_id = str(uuid.uuid4())
        _login(client, user_id)
        _patch_user_ctx(monkeypatch, user_id)

        b64 = base64.b64encode(b"ATOM      1  N   ALA A   1\n").decode()
        job = _job(
            user_id=user_id,
            candidates=[
                {"pdb_key": 0, "pdb_content_b64": b64},
                _candidate("design_2.pdb", b64=b64),
            ],
        )
        _patch_job(monkeypatch, job)
        monkeypatch.setattr(jobs_mod, "output_exists", lambda **_kw: False)

        resp = client.get(f"/api/jobs/{job.id}/pdb/0")
        assert resp.status_code == 404, (
            "a pdb_key of 0 was coerced to the matchable basename \"0\""
        )
