"""The bulk structure ZIP must not answer a total resolution failure with 200.

``/jobs/<id>/export.zip``, ``/campaigns/<id>/export.zip`` and
``/targets/<id>/export.zip`` all build their archive with
:func:`shared.exports.candidates_to_zip`, which skips any candidate whose
structure bytes do not resolve. A row KEEPS its ``pdb_key`` when the Storage
object behind it has been deleted, has expired, or is briefly unreachable, so
the page still renders that row's per-row ``.pdb`` link and its View 3D button
-- and the bulk button used to hand back HTTP 200 with a valid 22-byte archive
containing nothing at all.

What these tests pin is the discrimination between two skips that are
indistinguishable in the finished archive and are not the same event:

* A row referencing NO structure promised nothing, and an archive without it is
  a complete answer. ``tests/test_target_export.py::
  test_an_owned_but_empty_target_exports_an_empty_file_not_a_404`` requires that
  case to keep returning 200: a user whose runs have not yet produced a design
  must not be told their own target does not exist.
* A row that DOES carry a ``pdb_key`` or inline ``pdb_content_b64`` and still
  does not resolve is a design the surface offered and the file does not
  contain.

Only the second is an error, so emptiness alone cannot be the signal. The guard
reads ``candidates_to_zip``'s ``report`` rather than ``len(namelist())``, and
the test below that feeds a structureless row through the same route is what
stops a future "just 404 on an empty archive" from passing.

Storage is made to miss by raising :class:`StorageError` from
``download_output`` -- the same exception each route's ``_fetch`` already
catches, so the miss is handled by production code rather than by the test.
"""

from __future__ import annotations

import base64
import io
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import blueprints.jobs as jobs_mod

pytestmark = pytest.mark.usefixtures("isolate_supabase")


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


def _login(client, user_id="u-1"):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "u@example.com"
        sess["access_token"] = "fake-token"


def _ctx(user_id="u-1"):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=100, email="u@example.com",
    )


def _storage_miss(*_args, **_kwargs):
    """What a deleted / expired object or a Storage outage raises."""
    from shared.storage import StorageError
    raise StorageError("object not found")


def _stored(index, job="job-aaaa1111", tool=None):
    """A row whose structure lives in Storage only -- no inline copy.

    This is the shape the defect needs: the row still carries a pdb_key, so
    every per-row control on the page renders, and the bytes come from Storage
    at request time.
    """
    cand = {
        "pdb_key": f"designs/design_{index}.pdb",
        "sequence": "MKTAY",
        "scores": {"ipTM": 0.9},
        "_source_job_id": job,
        "_source_index": index,
        "_source_chunk": 0,
    }
    if tool:
        cand["_source_tool"] = tool
    return cand


def _inline(index, job="job-aaaa1111", tool=None):
    """A row that carries its own bytes and therefore always resolves."""
    cand = _stored(index, job=job, tool=tool)
    cand["pdb_content_b64"] = base64.b64encode(
        f"ATOM  {index}\n".encode()
    ).decode()
    return cand


def _job_row(candidates, user_id="u-1"):
    job = MagicMock()
    job.id = str(uuid.uuid4())
    job.user_id = user_id
    job.result = {"candidates": list(candidates)}
    job.tool = "pxdesign"
    return job


def _campaign_agg(candidates=(), **over):
    env = {
        "tool": "bindcraft", "candidates": list(candidates),
        "total": len(candidates), "capped": False, "partial": False,
    }
    env.update(over)
    return env


def _target_agg(candidates=(), **over):
    env = {
        "ok": True, "partial": False, "candidates": list(candidates),
        "total": len(candidates), "shown": len(candidates), "unranked": 0,
        "capped": False, "columns": [], "tools": ["bindcraft"], "per_tool": {},
        "campaigns": [], "standalone_jobs": 0, "refold_jobs": 0,
        "passed_total": 0, "provisional": False, "sort_mode": "percentile",
        "multi_tool": False, "limit": 300,
    }
    env.update(over)
    return env


_CID = str(uuid.uuid4())
_TID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Nothing resolves -> the download is refused, on all three routes.
#
# One route proves the shape; three prove the wiring. The guard is four lines
# hand-copied into each blueprint, so a test of one says nothing about the
# other two -- which is the failure mode a merged export is most exposed to.
# ---------------------------------------------------------------------------

def test_the_job_zip_refuses_when_every_structure_is_unresolved(
    client, monkeypatch,
):
    job = _job_row([_stored(1), _stored(2)])
    _login(client, job.user_id)
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: _ctx(job.user_id))
    monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)
    monkeypatch.setattr(jobs_mod, "download_output", _storage_miss)

    resp = client.get(f"/jobs/{job.id}/export.zip")

    assert resp.status_code == 409, resp.status_code
    assert resp.mimetype != "application/zip"
    body = resp.get_data(as_text=True)
    assert "None of the 2 designs" in body, body
    # The refusal has to be readable by the person who clicked, and it must
    # not send them looking for a fault in the scores or the other exports.
    assert "storage" in body.lower(), body
    assert "CSV" in body, body


def test_the_campaign_zip_refuses_when_every_structure_is_unresolved(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.compute_campaigns.aggregate_campaign_candidates",
                  return_value=_campaign_agg([_stored(1), _stored(2)])), \
            patch("shared.storage.download_output", _storage_miss):
        resp = client.get(f"/campaigns/{_CID}/export.zip")

    assert resp.status_code == 409, resp.status_code
    assert "None of the 2 designs" in resp.get_data(as_text=True)


def test_the_target_zip_refuses_when_every_structure_is_unresolved(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_target_agg([
                      _stored(1, job="job-bc", tool="bindcraft"),
                      _stored(1, job="job-bz", tool="boltzgen"),
                  ])), \
            patch("shared.storage.download_output", _storage_miss):
        resp = client.get(f"/targets/{_TID}/export.zip")

    assert resp.status_code == 409, resp.status_code
    assert "None of the 2 designs" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The two cases the guard must NOT catch.
# ---------------------------------------------------------------------------

def test_a_structureless_row_is_not_a_failure_and_still_exports_200(
    client, monkeypatch,
):
    """A row referencing no structure promised nothing.

    This is the half a ``len(namelist()) == 0`` guard would get wrong: the
    archive is byte-identical to the refused one above, and this user has
    nothing to be told. Sequence-only rows and a run that has not yet written
    a design both land here.
    """
    job = _job_row([{"sequence": "MKTAY", "scores": {"ipTM": 0.9}}])
    _login(client, job.user_id)
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: _ctx(job.user_id))
    monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)
    monkeypatch.setattr(jobs_mod, "download_output", _storage_miss)

    resp = client.get(f"/jobs/{job.id}/export.zip")

    assert resp.status_code == 200, resp.status_code
    assert resp.mimetype == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(resp.get_data())).namelist() == []


def test_a_job_with_no_candidates_at_all_still_exports_200(
    client, monkeypatch,
):
    """The job-route twin of test_target_export.py's owned-but-empty case."""
    job = _job_row([])
    _login(client, job.user_id)
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: _ctx(job.user_id))
    monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)

    resp = client.get(f"/jobs/{job.id}/export.zip")

    assert resp.status_code == 200, resp.status_code
    assert resp.mimetype == "application/zip"


# ---------------------------------------------------------------------------
# Partial resolution: ship the archive, but name what is absent.
# ---------------------------------------------------------------------------

def test_a_partial_archive_is_delivered_and_names_the_absent_designs(
    client, monkeypatch,
):
    """Some resolve, some do not.

    A short archive is still the best available answer, so it is delivered --
    but silently. The archive itself is the only channel a file download has,
    so the absent designs are named inside it.
    """
    job = _job_row([_inline(1), _stored(2), _stored(3)])
    _login(client, job.user_id)
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: _ctx(job.user_id))
    monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)
    monkeypatch.setattr(jobs_mod, "download_output", _storage_miss)

    resp = client.get(f"/jobs/{job.id}/export.zip")

    assert resp.status_code == 200, resp.status_code
    zf = zipfile.ZipFile(io.BytesIO(resp.get_data()))
    names = zf.namelist()
    assert "designs/design_1.pdb" in names, names
    assert "MISSING.txt" in names, names

    note = zf.read("MISSING.txt").decode()
    assert "2 of 3" in note, note
    # Named individually, not just counted -- "some designs are missing" does
    # not tell the customer which of their leads they still have to chase.
    assert "designs/design_2.pdb" in note, note
    assert "designs/design_3.pdb" in note, note
    # The design that DID resolve must not be listed as absent.
    assert "designs/design_1.pdb" not in note, note


def test_a_complete_archive_carries_no_missing_note(client, monkeypatch):
    """The note is a symptom of a partial archive, not a fixture of every ZIP.

    Without this, writing MISSING.txt unconditionally would pass every other
    test in this file.
    """
    job = _job_row([_inline(1), _inline(2)])
    _login(client, job.user_id)
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: _ctx(job.user_id))
    monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)

    resp = client.get(f"/jobs/{job.id}/export.zip")

    assert resp.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(resp.get_data())).namelist()
    assert names == ["designs/design_1.pdb", "designs/design_2.pdb"], names


def test_the_missing_note_namespaces_a_merged_target_export(client):
    """Across pooled tools the bare pdb_key does not identify a row.

    Every tool emits a design_1.pdb, so an unprefixed name in the note would
    not say WHICH design is absent. The note uses the arcname the design would
    have been archived under.
    """
    def _one_tool_only(*, user_id, job_id, filename):  # noqa: ARG001
        if job_id == "job-bc":
            return b"ATOM      1\n"
        _storage_miss()

    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_target_agg([
                      _stored(1, job="job-bc", tool="bindcraft"),
                      _stored(1, job="job-bz", tool="boltzgen"),
                  ])), \
            patch("shared.storage.download_output", _one_tool_only):
        resp = client.get(f"/targets/{_TID}/export.zip")

    assert resp.status_code == 200, resp.status_code
    zf = zipfile.ZipFile(io.BytesIO(resp.get_data()))
    assert "bindcraft/job-bc/designs/design_1.pdb" in zf.namelist()
    note = zf.read("MISSING.txt").decode()
    assert "boltzgen/job-bz/designs/design_1.pdb" in note, note
