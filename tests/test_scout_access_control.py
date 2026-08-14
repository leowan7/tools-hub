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

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from scout.jobs import (
    cleanup_old_jobs,
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
# Unit: the reaper stays inside its own data
# ---------------------------------------------------------------------------


def _age(path: Path, seconds: int) -> None:
    """Backdate ``path``'s mtime by ``seconds``."""
    old = time.time() - seconds
    os.utime(path, (old, old))


class TestCleanupOldJobsScope:
    """``cleanup_old_jobs`` reaps only the UUID dirs ``create_job_dir`` mints.

    ``tmp/`` is a SHARED scratch dir, not this module's private space. Before
    the name filter the reaper rmtree'd every immediate subdirectory older than
    an hour, and it fires as a side effect of three user-facing routes
    (/scout/upload, /scout/fetch-pdb, /scout/example) — so any signed-in user
    clicking "Try an example" reaped its neighbours. That is not hypothetical:
    it destroyed the tracked ``tmp/calibration/`` files once already (see
    tests/test_multichain_iptm_notice.py, which stubbed the reaper out of its
    page sweep rather than fixing it).

    The worst tenant to lose is ``tmp/pdb_compare/``: those fixtures are
    UNTRACKED and their tests ``pytest.skip`` when the files are missing rather
    than failing, so reaping them is silent, unrecoverable coverage loss.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "calibration",  # tracked provenance, the dir actually destroyed
            "pdb_compare",  # untracked fixtures; their tests skip, not fail
            "some-hand-made-dir",
            "3f2d1a0e",  # UUID-ish but too short
            "gggggggg-aaaa-bbbb-cccc-123456789012",  # UUID-shaped but non-hex
        ],
    )
    def test_non_uuid_dir_survives_however_old(self, tmp_path, name):
        tenant = tmp_path / name
        tenant.mkdir()
        payload = tenant / "dispatched.json"
        payload.write_text('{"kept": true}', encoding="utf-8")
        _age(tenant, 10 * 3600)

        assert cleanup_old_jobs(base_dir=tmp_path, max_age_seconds=3600) == 0
        assert tenant.is_dir()
        # The incident was the FILES one level down, not just the dir name.
        assert payload.read_text(encoding="utf-8") == '{"kept": true}'

    def test_old_uuid_job_dir_is_still_deleted(self, tmp_path):
        """The reaper must still reap — a filter that never deletes is no fix."""
        jid, job_dir = create_job_dir(USER_A, base_dir=tmp_path)
        (job_dir / "designs").mkdir()
        (job_dir / "designs" / "d.pdb").write_text("ATOM\n")
        _age(job_dir, 10 * 3600)

        assert cleanup_old_jobs(base_dir=tmp_path, max_age_seconds=3600) == 1
        assert not job_dir.exists()
        assert is_valid_job_id(jid)

    def test_fresh_uuid_job_dir_survives(self, tmp_path):
        _, job_dir = create_job_dir(USER_A, base_dir=tmp_path)

        assert cleanup_old_jobs(base_dir=tmp_path, max_age_seconds=3600) == 0
        assert job_dir.is_dir()

    def test_mixed_tree_reaps_only_the_old_job(self, tmp_path):
        """All three rules at once, which is the state the routes actually see."""
        tenant = tmp_path / "calibration"
        tenant.mkdir()
        _age(tenant, 10 * 3600)
        _, stale_job = create_job_dir(USER_A, base_dir=tmp_path)
        _age(stale_job, 10 * 3600)
        _, fresh_job = create_job_dir(USER_A, base_dir=tmp_path)

        assert cleanup_old_jobs(base_dir=tmp_path, max_age_seconds=3600) == 1
        assert tenant.is_dir()
        assert fresh_job.is_dir()
        assert not stale_job.exists()

    def test_unowned_job_dir_is_still_reaped(self, tmp_path):
        """Why the filter is the UUID shape and NOT the .owner marker.

        ``create_job_dir`` writes ``.owner`` only ``if owner:`` and
        ``write_owner`` swallows OSError, so an ownership-based filter would
        leak unowned dirs and failed marker-writes forever — the unbounded disk
        growth this reaper exists to prevent. The UUID shape is minted by
        construction, so it cannot leak.
        """
        _, job_dir = create_job_dir(None, base_dir=tmp_path)
        assert not (job_dir / ".owner").exists()
        _age(job_dir, 10 * 3600)

        assert cleanup_old_jobs(base_dir=tmp_path, max_age_seconds=3600) == 1
        assert not job_dir.exists()

    def test_loose_files_are_untouched(self, tmp_path):
        """The property the old docstring claimed — still true, just not the point."""
        loose = tmp_path / ".gitkeep"
        loose.write_text("", encoding="utf-8")
        uuid_named_file = tmp_path / str(uuid.uuid4())
        uuid_named_file.write_text("not a job", encoding="utf-8")
        _age(loose, 10 * 3600)
        _age(uuid_named_file, 10 * 3600)

        assert cleanup_old_jobs(base_dir=tmp_path, max_age_seconds=3600) == 0
        assert loose.is_file()
        assert uuid_named_file.is_file()


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
