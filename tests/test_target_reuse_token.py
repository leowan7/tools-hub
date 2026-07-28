"""The ``target:<uuid>`` reuse token on the atomic tool forms.

This is the seam that closes the loop: one uploaded target feeds both the
fan-out runs and single runs, with no re-upload either way. It is also the
most security-sensitive of the reuse tokens, because ``copy_input`` takes no
``source_user_id`` and ``download_input`` will read any object in the bucket —
the owner-scoped ``get_target`` is the entire tenancy boundary.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# These tests assert ownership and isolation, so they must not consult the
# live database that app.py's load_dotenv() would otherwise hand them.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.targets import DesignTarget


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_PXDESIGN", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _login(client, email="user@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def _ctx():
    return SimpleNamespace(
        user_id="u-test", tier="free", balance=100, email="user@example.com",
    )


def _synthetic_pdb(num_residues: int = 80, chain: str = "A") -> bytes:
    """Minimal-but-valid PDB with full N/CA/C/O backbones, so it survives both
    shared.pdb_inspect and the reuse hard-gate."""
    lines = ["HEADER    SYNTHETIC TEST"]
    atom_id = 0
    for resnum in range(1, num_residues + 1):
        for atom_name, dx in (("N", 0), ("CA", 1), ("C", 2), ("O", 3)):
            atom_id += 1
            x = float(resnum + dx)
            lines.append(
                f"ATOM  {atom_id:5d}  {atom_name:<3s} ALA {chain}{resnum:4d}"
                f"    {x:8.3f}{1.0:8.3f}{1.0:8.3f}  1.00 10.00           "
                f"{atom_name[0]}"
            )
    lines.append("END")
    return "\n".join(lines).encode()


def _target(**kw):
    base = dict(
        id=str(uuid.uuid4()),
        user_id="u-test",
        name="HER2",
        filename="her2.pdb",
        storage_path="u-test/target-abc/her2.pdb",
        target_chain="A",
    )
    base.update(kw)
    return DesignTarget(**base)


def _form(token, **kw):
    data = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "35,52,62",
        "binder_length": "40",
        "num_designs": "2",
        "reuse_pdb_token": token,
    }
    data.update(kw)
    return data


def test_target_token_copies_the_targets_structure_into_the_job(app, monkeypatch):
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: _ctx())
    t = _target()
    job = SimpleNamespace(
        id="job-stub", user_id="u-test", tool="pxdesign", preset="pilot",
        job_token="t" * 64, inputs={},
    )

    with patch("shared.targets.get_target", return_value=t) as fetch, \
            patch("blueprints.tools.create_job", return_value=job) as mk, \
            patch("blueprints.tools.copy_input",
                  return_value="u-test/job-stub/her2.pdb") as copied, \
            patch("blueprints.tools.upload_input") as uploaded, \
            patch("blueprints.tools.download_input",
                  return_value=_synthetic_pdb(80)), \
            patch("blueprints.tools.presigned_input_url",
                  return_value="https://u/x.pdb"), \
            patch("blueprints.tools.update_inputs"), \
            patch("blueprints.tools.set_modal_call"), \
            patch("gpu.modal_client.ModalClient.submit",
                  return_value={"function_call_id": "fc", "gpu_seconds_cap": 3600}):
        client = app.test_client()
        _login(client)
        resp = client.post(
            "/tools/pxdesign/submit",
            data=_form(f"target:{t.id}"),
            content_type="multipart/form-data",
        )

    assert resp.status_code in (302, 303)
    # No re-upload: the structure is copied from the target's prefix into the
    # job's, so the RLS owner-prefix still holds.
    uploaded.assert_not_called()
    assert copied.call_args.kwargs["source_path"] == t.storage_path
    assert copied.call_args.kwargs["dest_job_id"] == job.id
    assert copied.call_args.kwargs["dest_user_id"] == "u-test"
    # The job is target-attributable, which is the only way a standalone run
    # reaches its target's combined table.
    assert mk.call_args.kwargs["target_id"] == t.id
    # Owner scope enforced in the query, not after the fetch.
    assert fetch.call_args.kwargs["user_id"] == "u-test"


def test_an_unowned_target_is_rejected_before_any_row_or_hold(app, monkeypatch):
    """Rejecting after create_job would leave a pending orphan holding wallet
    funds — the same failure the missing-PDB gate exists to prevent."""
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: _ctx())

    with patch("shared.targets.get_target", return_value=None), \
            patch("blueprints.tools.create_job") as mk, \
            patch("blueprints.tools.copy_input") as copied, \
            patch("gpu.modal_client.ModalClient.submit") as submitted:
        client = app.test_client()
        _login(client)
        resp = client.post(
            "/tools/pxdesign/submit",
            data=_form(f"target:{uuid.uuid4()}"),
            content_type="multipart/form-data",
        )

    assert resp.status_code == 200
    assert "That target could not be found" in resp.get_data(as_text=True)
    mk.assert_not_called()
    copied.assert_not_called()
    submitted.assert_not_called()


def test_target_reuse_still_runs_the_hard_gate_on_the_copied_bytes(app, monkeypatch):
    """A reuse token skips the upload-boundary inspection, so the copied bytes
    are re-inspected before Modal. Here the target's structure has no chain A,
    which the run asks for: it must fail for $0 rather than on the GPU."""
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: _ctx())
    t = _target()
    job = SimpleNamespace(
        id="job-stub", user_id="u-test", tool="pxdesign", preset="pilot",
        job_token="t" * 64, inputs={},
    )

    with patch("shared.targets.get_target", return_value=t), \
            patch("blueprints.tools.create_job", return_value=job), \
            patch("blueprints.tools.copy_input",
                  return_value="u-test/job-stub/her2.pdb"), \
            patch("blueprints.tools.download_input",
                  return_value=_synthetic_pdb(80, chain="B")), \
            patch("blueprints.tools.presigned_input_url",
                  return_value="https://u/x.pdb"), \
            patch("blueprints.tools.mark_failed") as failed, \
            patch("gpu.modal_client.ModalClient.submit") as submitted:
        client = app.test_client()
        _login(client)
        resp = client.post(
            "/tools/pxdesign/submit",
            data=_form(f"target:{t.id}"),
            content_type="multipart/form-data",
        )

    assert resp.status_code == 200
    submitted.assert_not_called()
    failed.assert_called_once()
    detail = failed.call_args.kwargs["error"]["detail"]
    # Specifically the chain gate, not some other failure that happens to
    # reach mark_failed (a presign error did exactly that while writing this).
    assert "is not in the uploaded file" in detail


def test_an_archived_target_is_rejected(app, monkeypatch):
    """An archived target is excluded from the retention sweeper's protected
    set, so its structure may already be deleted. Accepting one here creates a
    job row, copies nothing, and dies in Storage. /campaigns rejects the same
    id, and the two routes must not disagree about what is launchable."""
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: _ctx())

    with patch("shared.targets.get_target",
               return_value=_target(archived_at="2026-07-01T00:00:00Z")), \
            patch("blueprints.tools.create_job") as mk, \
            patch("blueprints.tools.copy_input") as copied, \
            patch("gpu.modal_client.ModalClient.submit") as submitted:
        client = app.test_client()
        _login(client)
        resp = client.post(
            "/tools/pxdesign/submit",
            data=_form(f"target:{uuid.uuid4()}"),
            content_type="multipart/form-data",
        )

    assert resp.status_code == 200
    assert "That target is archived" in resp.get_data(as_text=True)
    mk.assert_not_called()
    copied.assert_not_called()
    submitted.assert_not_called()


def test_an_attached_file_overrides_the_token_and_drops_the_target_link(
    app, monkeypatch,
):
    """Override-by-upload is the documented behaviour for every reuse token
    (templates/tools/_prefill.html says so verbatim). The target's structure is
    then never staged, so the run must NOT be filed under it — a design
    produced from a different structure appearing in that target's merged
    ranking is worse than one that is simply unparented."""
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: _ctx())
    t = _target()
    job = SimpleNamespace(
        id="job-stub", user_id="u-test", tool="pxdesign", preset="pilot",
        job_token="t" * 64, inputs={},
    )

    with patch("shared.targets.get_target", return_value=t), \
            patch("blueprints.tools.create_job", return_value=job) as mk, \
            patch("blueprints.tools.copy_input") as copied, \
            patch("blueprints.tools.upload_input",
                  return_value="u-test/job-stub/mine.pdb") as uploaded, \
            patch("blueprints.tools.presigned_input_url",
                  return_value="https://u/x.pdb"), \
            patch("blueprints.tools.update_inputs"), \
            patch("blueprints.tools.set_modal_call"), \
            patch("gpu.modal_client.ModalClient.submit",
                  return_value={"function_call_id": "fc", "gpu_seconds_cap": 3600}):
        client = app.test_client()
        _login(client)
        resp = client.post(
            "/tools/pxdesign/submit",
            data=dict(
                _form(f"target:{t.id}"),
                target_pdb=(io.BytesIO(_synthetic_pdb(80)), "mine.pdb"),
            ),
            content_type="multipart/form-data",
        )

    assert resp.status_code in (302, 303)
    # The uploaded file ran, not the target's structure.
    uploaded.assert_called_once()
    copied.assert_not_called()
    # And the job is NOT filed under the target it never touched.
    assert mk.call_args.kwargs["target_id"] is None


def test_a_target_with_no_staged_structure_is_rejected(app, monkeypatch):
    """A structure-less target (proteina's curated path) cannot satisfy a tool
    that requires a PDB."""
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: _ctx())

    with patch("shared.targets.get_target",
               return_value=_target(storage_path=None)), \
            patch("blueprints.tools.create_job") as mk:
        client = app.test_client()
        _login(client)
        resp = client.post(
            "/tools/pxdesign/submit",
            data=_form(f"target:{uuid.uuid4()}"),
            content_type="multipart/form-data",
        )

    assert resp.status_code == 200
    assert "That target could not be found" in resp.get_data(as_text=True)
    mk.assert_not_called()
