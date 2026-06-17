"""Access-control tests for the Scout blueprint (audit 2026-06-17, M1).

Locks the fix for the cross-user IDOR + constrained path-traversal in the
Scout file routes. Before the fix, ``/scout/pdb/<job_id>``,
``/scout/download/<job_id>``, ``/scout/feasibility/download/<job_id>`` and
the handoff built ``tmp/<job_id>`` straight from user input — no UUID
check, no ``safe_join`` confinement, no ownership check.

Three properties are asserted:
  (a) a logged-in user cannot read another user's job (404),
  (b) a non-UUID job id is rejected (404),
  (c) a ``../``-style job id is rejected (404 / None).

The unit tests exercise ``scout.jobs`` directly; the route tests drive the
Flask app the same way ``tests/test_404_route.py`` does — ``create_app``
with only ``SESSION_SECRET_KEY`` set, login faked via ``session_transaction``.
None of these tests touch Supabase: every rejection happens at the
validate / confine / ownership gate before any client is built.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from scout.jobs import (
    create_job_dir,
    is_valid_job_id,
    read_owner,
    resolve_owned_job_dir,
    safe_job_dir,
)

USER_A = "user-a-11111111"
USER_B = "user-b-22222222"


# ---------------------------------------------------------------------------
# Unit: UUID validation
# ---------------------------------------------------------------------------


class TestJobIdValidation:
    def test_real_uuid_is_valid(self):
        assert is_valid_job_id(str(uuid.uuid4()))

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-uuid",
            "",
            "../etc/passwd",
            "..",
            "3f2d1a0e",  # too short
            "3f2d1a0e-aaaa-bbbb-cccc-1234567890ab/../x",  # trailing traversal
            "gggggggg-aaaa-bbbb-cccc-123456789012",  # non-hex
            None,
            123,
        ],
    )
    def test_rejects_non_uuid(self, bad):
        assert not is_valid_job_id(bad)


# ---------------------------------------------------------------------------
# Unit: safe_join confinement
# ---------------------------------------------------------------------------


class TestSafeJobDir:
    def test_valid_uuid_confined_under_base(self, tmp_path):
        jid = str(uuid.uuid4())
        assert safe_job_dir(jid, base_dir=tmp_path) == tmp_path / jid

    @pytest.mark.parametrize(
        "bad",
        [
            "../etc",
            "../../etc/passwd",
            "/etc/passwd",
            "..\\..\\windows",
            "not-a-uuid",
        ],
    )
    def test_traversal_and_non_uuid_return_none(self, bad, tmp_path):
        assert safe_job_dir(bad, base_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Unit: ownership gate
# ---------------------------------------------------------------------------


class TestResolveOwnedJobDir:
    def test_create_job_dir_records_owner(self, tmp_path):
        _, job_dir = create_job_dir(USER_A, base_dir=tmp_path)
        assert read_owner(job_dir) == USER_A

    def test_owner_match_returns_dir(self, tmp_path):
        jid, job_dir = create_job_dir(USER_A, base_dir=tmp_path)
        assert resolve_owned_job_dir(jid, USER_A, base_dir=tmp_path) == job_dir

    def test_owner_mismatch_returns_none(self, tmp_path):
        jid, _ = create_job_dir(USER_A, base_dir=tmp_path)
        assert resolve_owned_job_dir(jid, USER_B, base_dir=tmp_path) is None

    def test_missing_owner_marker_fails_closed(self, tmp_path):
        # A legacy / unowned dir (created with no owner) is inaccessible.
        jid, _ = create_job_dir(None, base_dir=tmp_path)
        assert resolve_owned_job_dir(jid, USER_A, base_dir=tmp_path) is None

    def test_empty_owner_arg_returns_none(self, tmp_path):
        jid, _ = create_job_dir(USER_A, base_dir=tmp_path)
        assert resolve_owned_job_dir(jid, "", base_dir=tmp_path) is None

    def test_nonexistent_dir_returns_none(self, tmp_path):
        assert (
            resolve_owned_job_dir(str(uuid.uuid4()), USER_A, base_dir=tmp_path)
            is None
        )

    def test_traversal_id_returns_none(self, tmp_path):
        assert resolve_owned_job_dir("../../etc", USER_A, base_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Route-level: the live Scout blueprint under the real tmp/ tree
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _login(client, *, user_id, email="someone@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email
        sess["user_id"] = user_id


@pytest.fixture
def owned_job():
    """A real ``tmp/<uuid>`` owned by USER_A with the files the routes serve.

    Routes resolve ``Path("tmp")`` relative to cwd (the worktree root under
    pytest), so the dir must live in the real tree, not pytest's tmp_path.
    """
    base = Path("tmp")
    base.mkdir(parents=True, exist_ok=True)
    jid, job_dir = create_job_dir(USER_A, base_dir=base)
    (job_dir / "input.pdb").write_text(
        "ATOM      1  N   MET A   1       0.000   0.000   0.000\n"
    )
    (job_dir / "epitopes.csv").write_text('epitope_id,residues\n1,"10,11,12"\n')
    (job_dir / "feasibility_results.csv").write_text(
        "composite_feasibility,tier\n0.5,Moderate\n"
    )
    try:
        yield jid, job_dir
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


class TestServePdbAccessControl:
    def test_owner_can_read(self, app, owned_job):
        jid, _ = owned_job
        client = app.test_client()
        _login(client, user_id=USER_A)
        assert client.get(f"/scout/pdb/{jid}").status_code == 200

    def test_other_user_gets_404(self, app, owned_job):
        jid, _ = owned_job
        client = app.test_client()
        _login(client, user_id=USER_B)
        assert client.get(f"/scout/pdb/{jid}").status_code == 404

    def test_non_uuid_rejected(self, app):
        client = app.test_client()
        _login(client, user_id=USER_A)
        assert client.get("/scout/pdb/not-a-uuid").status_code == 404


class TestDownloadAccessControl:
    def test_owner_can_download(self, app, owned_job):
        jid, _ = owned_job
        client = app.test_client()
        _login(client, user_id=USER_A)
        assert client.get(f"/scout/download/{jid}").status_code == 200

    def test_other_user_gets_404(self, app, owned_job):
        jid, _ = owned_job
        client = app.test_client()
        _login(client, user_id=USER_B)
        assert client.get(f"/scout/download/{jid}").status_code == 404

    def test_non_uuid_rejected(self, app):
        client = app.test_client()
        _login(client, user_id=USER_A)
        assert client.get("/scout/download/not-a-uuid").status_code == 404


class TestFeasibilityDownloadAccessControl:
    def test_owner_can_download(self, app, owned_job):
        jid, _ = owned_job
        client = app.test_client()
        _login(client, user_id=USER_A)
        assert client.get(f"/scout/feasibility/download/{jid}").status_code == 200

    def test_other_user_gets_404(self, app, owned_job):
        jid, _ = owned_job
        client = app.test_client()
        _login(client, user_id=USER_B)
        assert client.get(f"/scout/feasibility/download/{jid}").status_code == 404


class TestHandoffAccessControl:
    def test_traversal_scout_job_id_rejected(self, app):
        client = app.test_client()
        _login(client, user_id=USER_A)
        resp = client.post(
            "/scout/handoff/tool",
            data={
                "tool": "rfantibody",
                "scout_job_id": "../../etc/passwd",
                "hotspot_residues": "10,11",
            },
        )
        assert resp.status_code == 404

    def test_non_uuid_scout_job_id_rejected(self, app):
        client = app.test_client()
        _login(client, user_id=USER_A)
        resp = client.post(
            "/scout/handoff/tool",
            data={
                "tool": "rfantibody",
                "scout_job_id": "not-a-uuid",
                "hotspot_residues": "10,11",
            },
        )
        assert resp.status_code == 404

    def test_other_user_gets_404(self, app, owned_job):
        jid, _ = owned_job
        client = app.test_client()
        _login(client, user_id=USER_B)
        resp = client.post(
            "/scout/handoff/tool",
            data={
                "tool": "rfantibody",
                "scout_job_id": jid,
                "hotspot_residues": "10,11",
            },
        )
        assert resp.status_code == 404
