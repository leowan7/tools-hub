"""Submit-route orphan-row prevention.

Production incident 2026-04-30: a pxdesign pilot POST without a PDB
file attached created tool_jobs row ``d2d421ad-...`` in ``pending``
status, but never reached ``modal_client.submit`` (the upload check at
line 1013 returned early). The row sat for 2.5 hours with
``modal_function_call_id IS NULL`` until the user manually cancelled
it. Cancel then refunded 15 credits that ``record_spend`` never
debited — net 15 free credits to the user.

The fix moves the "PDB source provided?" gate BEFORE ``create_job`` so
a no-source submission re-renders the form without writing a row.

These tests pin that ordering and assert two invariants:

  1. ``POST /tools/<requires_pdb>/submit`` with no file and no
     ``reuse_pdb_token`` does NOT call ``create_job`` — and returns
     the form with the "Upload a target PDB file." error.
  2. ``POST`` with a file present DOES call ``create_job`` (otherwise
     the early gate is too aggressive and breaks the happy path).
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _synthetic_pdb_bytes(num_residues: int = 80, chain: str = "A") -> bytes:
    """Produce a minimal-but-valid PDB with ``num_residues`` ALA on chain.

    Each residue has full N/CA/C/O backbone with non-zero coords so it
    survives shared.pdb_inspect's checks AND the GPU-side
    pipeline_normalize.normalize_for_pipeline backbone validator.
    """
    lines: list = ["HEADER    SYNTHETIC TEST"]
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


@pytest.fixture
def app_with_pxdesign_flag(monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_PXDESIGN", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login_session(client, email="user@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        user_id="u-test", tier="free", balance=100, email="user@example.com"
    )


def _bypass_idempotent(monkeypatch):
    """Replace @idempotent with a passthrough so the test exercises the
    handler directly. The decorator is applied at create_app() time, so
    we have to patch it BEFORE the app boots — not used in current tests
    but kept for documentation. We instead let the handler run; missing-
    file submits don't write idempotency rows that affect later asserts.
    """
    return None


class TestPxdesignMissingPdbDoesNotOrphan:
    """``POST /tools/pxdesign/submit`` with no file MUST NOT create a row."""

    def test_no_file_no_reuse_token_skips_create_job(
        self, app_with_pxdesign_flag, monkeypatch
    ):
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())
        # The bug repro: user submits the form without attaching a file.
        # The form has all the text fields but the file input is empty.
        with patch("app.create_job") as create_job, patch(
            "gpu.modal_client.ModalClient.submit"
        ) as modal_submit:
            client = app_with_pxdesign_flag.test_client()
            _login_session(client)
            resp = client.post(
                "/tools/pxdesign/submit",
                data={
                    "preset": "pilot",
                    "target_chain": "A",
                    "hotspot_residues": "35,52,62",
                    "binder_length": "40",
                    "num_designs": "2",
                },
                content_type="multipart/form-data",
            )
        # Server re-renders the form with the "Upload a target PDB file."
        # error and never calls create_job.
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Upload a target PDB file" in body
        create_job.assert_not_called()
        # Modal client must not have been touched either — no submit, no
        # function_call_id to track.
        modal_submit.assert_not_called()

    def test_with_file_attached_does_call_create_job(
        self, app_with_pxdesign_flag, monkeypatch
    ):
        """Defensive: the early gate must not block the happy path."""
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())

        # Make create_job return a stub job so the handler continues into
        # the upload / Modal-submit code, which we then short-circuit on
        # the upload_input mock.
        fake_job = SimpleNamespace(
            id="job-stub",
            user_id="u-test",
            tool="pxdesign",
            preset="pilot",
            credits_cost=15,
            job_token="t" * 64,
            inputs={},
        )

        with patch("app.create_job", return_value=fake_job) as create_job, \
             patch("app.upload_input", return_value="path/x.pdb") as upload_input, \
             patch("app.presigned_input_url", return_value="https://u/x.pdb"), \
             patch("app.update_inputs"), \
             patch("app.set_modal_call"), \
             patch("gpu.modal_client.ModalClient.submit") as modal_submit:
            modal_submit.return_value = {
                "function_call_id": "fc-test", "gpu_seconds_cap": 3600,
            }
            client = app_with_pxdesign_flag.test_client()
            _login_session(client)
            resp = client.post(
                "/tools/pxdesign/submit",
                data={
                    "preset": "pilot",
                    "target_chain": "A",
                    "hotspot_residues": "35,52,62",
                    "binder_length": "40",
                    "num_designs": "2",
                    # Synthetic 80-residue chain A so hotspots 35/52/62
                    # fall in range under the new pre-flight validator
                    # (shared/pdb_inspect.py).
                    "target_pdb": (io.BytesIO(_synthetic_pdb_bytes(80)),
                                   "test.pdb"),
                },
                content_type="multipart/form-data",
            )
        # Happy path: redirect to /jobs/<id>.
        assert resp.status_code in (302, 303)
        create_job.assert_called_once()
        upload_input.assert_called_once()
        modal_submit.assert_called_once()


class TestNonPdbToolNotAffected:
    """A tool that does NOT require a PDB (af2 — FASTA-only) must
    continue to work — the early gate keys off ``adapter.requires_pdb``
    AND ``preset.requires_pdb``, so a no-pdb tool is never gated."""

    def test_af2_smoke_does_not_require_pdb_upload(self, monkeypatch):
        monkeypatch.setenv("FLAG_TOOL_AF2", "on")
        monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
        from app import create_app
        flask_app = create_app()
        flask_app.config["TESTING"] = True
        monkeypatch.setattr("app.load_user_context", lambda: _ctx())

        fake_job = SimpleNamespace(
            id="job-af2",
            user_id="u-test",
            tool="af2",
            preset="smoke",
            credits_cost=0,
            job_token="t" * 64,
            inputs={},
        )
        with patch("app.create_job", return_value=fake_job), \
             patch("app.set_modal_call"), \
             patch("gpu.modal_client.ModalClient.submit") as modal_submit:
            modal_submit.return_value = {
                "function_call_id": "fc-af2", "gpu_seconds_cap": 180,
            }
            client = flask_app.test_client()
            _login_session(client)
            resp = client.post(
                "/tools/af2/submit",
                data={
                    "preset": "smoke",
                    # AF2 takes FASTA inline — no PDB anywhere.
                    "fasta_sequence": ">x\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTL",
                },
                content_type="multipart/form-data",
            )
        # We don't care about the exact response shape here — the assertion
        # is just that the early gate did not block this no-PDB tool.
        # create_job should have been reached (or not, if validate caught
        # something) but NOT short-circuited with "Upload a target PDB file."
        body = resp.get_data(as_text=True)
        assert "Upload a target PDB file" not in body
