"""Unit tests for the D4 - ESMFold standalone atomic tool.

Mirrors ``tests/test_colabfold_smoke.py`` end-to-end:

1. The adapter registers with the right slug, presets, credit costs.
2. ``validate()`` accepts well-formed input and rejects every known
   malformed case (missing FASTA, bad residues, oversized sequences,
   and - ESMFold-specific - any multimer input).
3. ``build_payload()`` produces the expected job_spec shape with
   ``fasta_text`` delivered inline (no file upload).
4. The Flask form template renders and submit validation rejects
   malformed data (feature flag must be flipped ON in the test
   process so the route is not 404'd).
5. The Modal webhook handler accepts a well-formed COMPLETED POST for
   an ESMFold job and rejects replay / unknown-job / bad-token cases.
6. The stub-rejection guard trips on every known silent-stub signature
   (uniform pLDDT, NaN pLDDT, implausible mean, degenerate PDB).

Runs fully offline - no Modal, no Supabase, no GPU. Uses the same
monkey-patching pattern as ``tests/test_colabfold_smoke.py``.
"""

from __future__ import annotations

import base64
import json

import pytest

from tools import esmfold as esm_mod
from tools.base import get as get_adapter


# ---------------------------------------------------------------------------
# Test 1 - adapter registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_adapter_registered_under_esmfold_slug(self):
        adapter = get_adapter("esmfold")
        assert adapter is not None, "tools.esmfold did not register its adapter"
        assert adapter.slug == "esmfold"

    def test_presets_shape(self):
        adapter = get_adapter("esmfold")
        slugs = [p.slug for p in adapter.presets]
        assert slugs == ["smoke", "standalone"]

    def test_credit_costs_match_atomic_spec(self):
        """ATOMIC-TOOLS.md D4 + PRODUCT-PLAN.md: smoke=0, standalone=1."""
        adapter = get_adapter("esmfold")
        smoke = adapter.preset_for("smoke")
        standalone = adapter.preset_for("standalone")
        assert smoke.credits_cost == 0
        assert standalone.credits_cost == 1

    def test_neither_preset_requires_pdb(self):
        """ESMFold takes FASTA text, never a PDB upload."""
        adapter = get_adapter("esmfold")
        assert adapter.requires_pdb is False
        for p in adapter.presets:
            assert p.requires_pdb is False

    def test_templates_point_at_esmfold_partials(self):
        adapter = get_adapter("esmfold")
        assert adapter.form_template == "tools/esmfold_form.html"
        assert adapter.results_partial == "tools/esmfold_results.html"


# ---------------------------------------------------------------------------
# Test 2 - validate() happy path + rejections
# ---------------------------------------------------------------------------


UBIQUITIN = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"


class TestValidate:
    def test_rejects_empty_preset(self):
        inputs, err = esm_mod.validate({}, {})
        assert inputs is None
        assert "preset" in (err or "").lower()

    def test_rejects_unknown_preset(self):
        inputs, err = esm_mod.validate({"preset": "full"}, {})
        assert inputs is None

    def test_smoke_preset_happy_path(self):
        inputs, err = esm_mod.validate({"preset": "smoke"}, {})
        assert err is None
        assert inputs["preset"] == "smoke"
        assert inputs["fasta_text"] == ""

    def test_standalone_happy_path_with_header(self):
        form = {
            "preset": "standalone",
            "fasta_text": f">ubiquitin\n{UBIQUITIN}",
        }
        inputs, err = esm_mod.validate(form, {})
        assert err is None, err
        assert inputs["preset"] == "standalone"
        assert ">ubiquitin" in inputs["fasta_text"]
        assert UBIQUITIN in inputs["fasta_text"]

    def test_standalone_accepts_bare_sequence(self):
        """Bare sequence (no >header) gets normalised to include a header."""
        form = {"preset": "standalone", "fasta_text": UBIQUITIN}
        inputs, err = esm_mod.validate(form, {})
        assert err is None, err
        assert inputs["fasta_text"].startswith(">")
        assert UBIQUITIN in inputs["fasta_text"]

    def test_standalone_rejects_empty_fasta(self):
        form = {"preset": "standalone", "fasta_text": "   "}
        inputs, err = esm_mod.validate(form, {})
        assert inputs is None
        assert err is not None

    def test_standalone_rejects_non_canonical_residues(self):
        """B, J, O, U, Z, * are not in the canonical 20 + X alphabet."""
        form = {
            "preset": "standalone",
            "fasta_text": f">bad\n{UBIQUITIN[:40]}BJOZ{UBIQUITIN[40:]}",
        }
        inputs, err = esm_mod.validate(form, {})
        assert inputs is None
        assert "non-canonical" in (err or "")

    def test_standalone_rejects_oversized_sequence(self):
        """> 400 aa must be rejected at form validation time."""
        giant = "A" * 500
        form = {"preset": "standalone", "fasta_text": f">big\n{giant}"}
        inputs, err = esm_mod.validate(form, {})
        assert inputs is None
        assert "max" in (err or "").lower()

    def test_standalone_rejects_tiny_sequence(self):
        form = {"preset": "standalone", "fasta_text": ">tiny\nAAAA"}
        inputs, err = esm_mod.validate(form, {})
        assert inputs is None

    def test_standalone_rejects_multimer_multiple_records(self):
        """ESMFold v1 is monomer-only - reject multi-record FASTA."""
        chain_a = "A" * 60
        chain_b = "L" * 60
        fasta = f">chainA\n{chain_a}\n>chainB\n{chain_b}"
        form = {"preset": "standalone", "fasta_text": fasta}
        inputs, err = esm_mod.validate(form, {})
        assert inputs is None, "multi-record FASTA should be rejected"
        assert "monomer" in (err or "").lower()

    def test_standalone_rejects_multimer_colon_separator(self):
        """':' chain separator (ColabFold convention) must also be rejected."""
        chain_a = "A" * 60
        chain_b = "L" * 60
        fasta = f">combined\n{chain_a}:{chain_b}"
        form = {"preset": "standalone", "fasta_text": fasta}
        inputs, err = esm_mod.validate(form, {})
        assert inputs is None, "':' multimer separator should be rejected"
        assert "monomer" in (err or "").lower() or ":" in (err or "")

    def test_standalone_monomer_single_record_stays_single(self):
        """Single-chain input stays as exactly one FASTA record, no ``:``."""
        form = {"preset": "standalone", "fasta_text": f">protA\n{UBIQUITIN}"}
        inputs, err = esm_mod.validate(form, {})
        assert err is None, err
        assert inputs["fasta_text"].count(">") == 1
        assert ":" not in inputs["fasta_text"]


# ---------------------------------------------------------------------------
# Test 3 - build_payload() shape
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_smoke_payload_shape(self):
        inputs, _ = esm_mod.validate({"preset": "smoke"}, {})
        payload = esm_mod.build_payload(inputs, presigned_url="")
        assert payload["fasta_text"] == ""
        # ESMFold has no tunable parameters - parameters dict is empty.
        assert payload["parameters"] == {}

    def test_standalone_payload_shape(self):
        inputs, _ = esm_mod.validate(
            {
                "preset": "standalone",
                "fasta_text": f">x\n{UBIQUITIN}",
            },
            {},
        )
        payload = esm_mod.build_payload(inputs, presigned_url="https://ignored")
        assert payload["fasta_text"].startswith(">")
        assert UBIQUITIN in payload["fasta_text"]
        # FASTA travels inline - no presigned URL embedded.
        assert "https://ignored" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Test 4 - Flask form + submit validation
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_esmfold_flag(monkeypatch):
    """Boot the tools-hub Flask app with FLAG_TOOL_ESMFOLD=on so the
    route resolves rather than 404s."""
    monkeypatch.setenv("FLAG_TOOL_ESMFOLD", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login_session(client, email="user@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def test_form_renders_when_flag_on(app_with_esmfold_flag, monkeypatch):
    """GET /tools/esmfold renders the form when the flag is flipped on."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_esmfold_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/esmfold")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ESMFold" in body
    assert "Smoke" in body
    assert "Standalone" in body


def test_form_404s_when_flag_off(app_with_esmfold_flag, monkeypatch):
    """With the flag removed, the route must 404 - launch-gate contract."""
    monkeypatch.delenv("FLAG_TOOL_ESMFOLD", raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_esmfold_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/esmfold")
    assert resp.status_code == 404


def test_submit_rejects_unknown_preset(app_with_esmfold_flag, monkeypatch):
    """POST with a bad preset rerenders the form with the validation error."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_esmfold_flag.test_client()
    _login_session(client)
    resp = client.post(
        "/tools/esmfold/submit",
        data={"preset": "bogus"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "preset" in body.lower()


def test_handoff_pilot_preset_maps_to_standalone_not_smoke(
    app_with_esmfold_flag, monkeypatch
):
    """Cross-tool ``from_job`` handoff sets pre_fill['preset']='pilot'.
    ESMFold has no 'pilot' option. Template must remap to 'standalone'
    so the incoming sequence is actually used - otherwise ``loop.first``
    silently selects 'smoke' and folds the baked ubiquitin fixture
    instead of the caller's sequence."""
    import re
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    mock_src = SimpleNamespace(
        id="src-job-abc",
        tool="boltzgen",
        inputs={"target_chain": "A"},
    )
    monkeypatch.setattr(
        "app.get_job",
        lambda job_id, user_id: mock_src if job_id == "src-job-abc" else None,
    )
    client = app_with_esmfold_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/esmfold?from_job=src-job-abc")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    smoke_opt = re.search(r'<option value="smoke"[^>]*>', body)
    standalone_opt = re.search(r'<option value="standalone"[^>]*>', body)
    assert smoke_opt and standalone_opt, "expected both preset options rendered"
    assert "selected" in standalone_opt.group(0), (
        "pilot->standalone remap failed: standalone option not selected"
    )
    assert "selected" not in smoke_opt.group(0), (
        "smoke option should not be selected on a handoff"
    )


# ---------------------------------------------------------------------------
# Test 5 - Modal webhook handler accepts/rejects correctly
# ---------------------------------------------------------------------------


class TestWebhookRoundtrip:
    """Exercise the shared webhook handler against an ESMFold job. We
    do not test the full complete_job pipeline (Supabase dependency);
    we test the handler's response to each auth state and to each
    payload status."""

    def _fake_job(self, status="running", token="t" * 64, tool="esmfold"):
        from types import SimpleNamespace
        return SimpleNamespace(
            id="job-uuid-1",
            job_token=token,
            status=status,
            tool=tool,
        )

    def test_rejects_unknown_job(self, app_with_esmfold_flag, monkeypatch):
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: None)
        client = app_with_esmfold_flag.test_client()
        resp = client.post(
            "/webhooks/modal/missing-job/some-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 404

    def test_rejects_bad_token(self, app_with_esmfold_flag, monkeypatch):
        fake = self._fake_job(token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_esmfold_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/wrong-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 403

    def test_accepts_completed_with_good_token(
        self, app_with_esmfold_flag, monkeypatch
    ):
        fake = self._fake_job(status="running", token="good-token")
        fresh = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        monkeypatch.setattr(
            "webhooks.modal.complete_job", lambda *a, **kw: fresh
        )
        client = app_with_esmfold_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={
                "status": "COMPLETED",
                "output": {
                    "pdb_b64": "TU9DS19QREI=",
                    "plddt_per_residue": [80.0, 82.5, 81.0],
                    "mean_plddt": 81.17,
                    "ptm": None,
                    "runtime_seconds": 30,
                },
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "recorded"

    def test_replay_on_terminal_is_noop(
        self, app_with_esmfold_flag, monkeypatch
    ):
        """Replaying the same POST after the job is already terminal
        must not mutate state - returns ``already_terminal``."""
        fake = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_esmfold_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "already_terminal"


# ---------------------------------------------------------------------------
# Test 6 - modal_client PRESET_CAPS + APP_NAME_OVERRIDES wiring
# ---------------------------------------------------------------------------


class TestSmokePresetShape:
    def test_app_name_override_maps_esmfold_to_ranomics_namespace(self):
        """Sanity: slug "esmfold" resolves to ``ranomics-esmfold-prod``."""
        from gpu.modal_client import modal_app_name

        assert modal_app_name("esmfold") == "ranomics-esmfold-prod"
        # Non-overridden tools keep the kendrew- prefix.
        assert modal_app_name("bindcraft") == "kendrew-bindcraft-prod"

    def test_preset_gpu_seconds_caps_registered(self):
        """Both ESMFold presets have an entry in PRESET_CAPS - the
        generic submit route raises ``ValueError`` otherwise."""
        from gpu.modal_client import preset_gpu_seconds

        assert preset_gpu_seconds("esmfold", "smoke") == 90
        assert preset_gpu_seconds("esmfold", "standalone") == 360

    def test_legacy_fast_preset_still_registered(self):
        """Pre-D4 planning code paths used the ``fast`` preset alias.
        Keep it registered so any stale job reference doesn't 500."""
        from gpu.modal_client import preset_gpu_seconds

        assert preset_gpu_seconds("esmfold", "fast") == 360

    def test_modal_payload_for_smoke_offline_stub(self, monkeypatch):
        """With modal patched to None, submit returns the deterministic stub."""
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = esm_mod.validate({"preset": "smoke"}, {})
        payload = esm_mod.build_payload(inputs, presigned_url="")
        result = client.submit(
            "esmfold",
            "smoke",
            inputs={**payload, "_input_presigned_url": ""},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="",
        )
        assert result["function_call_id"].startswith(
            "fc-stub-esmfold-smoke-"
        )
        assert result["gpu_seconds_cap"] == 90

    def test_modal_payload_for_standalone_offline_stub(self, monkeypatch):
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = esm_mod.validate(
            {
                "preset": "standalone",
                "fasta_text": f">x\n{UBIQUITIN}",
            },
            {},
        )
        payload = esm_mod.build_payload(inputs, presigned_url="")
        result = client.submit(
            "esmfold",
            "standalone",
            inputs={**payload, "_input_presigned_url": ""},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="https://tools/webhook",
        )
        assert result["function_call_id"].startswith(
            "fc-stub-esmfold-standalone-"
        )
        assert result["gpu_seconds_cap"] == 360


# ---------------------------------------------------------------------------
# Test 7 - run_pipeline.py stub rejection (no GPU -> parser-only tests)
# ---------------------------------------------------------------------------
#
# The full run_pipeline.main() requires a GPU and a 15 GB ESMFold
# checkpoint, so we can only exercise the stub-rejection + output-shaping
# logic here. The other half (preflight, model load, forward pass) is
# covered by the live smoke validation the user owes on Modal.


# A realistic-looking PDB string used by stub tests to exercise the
# ATOM-parse branch of reject_stub().
_FAKE_PDB = (
    "HEADER    TEST PDB                                "
    "20-APR-26   XXXX\n"
    + "ATOM      1  CA  ALA A   1      10.000  20.000  30.000  "
      "1.00 80.00           C\n"
    + "ATOM      2  CA  GLY A   2      11.500  21.500  31.500  "
      "1.00 82.00           C\n"
    + "ATOM      3  CA  LEU A   3      13.000  22.500  32.000  "
      "1.00 78.00           C\n"
    + "ATOM      4  CA  VAL A   4      14.500  23.500  33.000  "
      "1.00 85.00           C\n"
    + "ATOM      5  CA  PRO A   5      16.000  24.500  34.000  "
      "1.00 83.00           C\n"
    + "END\n"
)
_FAKE_PDB_B64 = base64.b64encode(_FAKE_PDB.encode("utf-8")).decode("ascii")

_ZERO_PDB = (
    "HEADER    ZERO\n"
    + "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  "
      "1.00 80.00           C\n"
    + "ATOM      2  CA  GLY A   2       0.000   0.000   0.000  "
      "1.00 82.00           C\n"
    + "ATOM      3  CA  LEU A   3       0.000   0.000   0.000  "
      "1.00 78.00           C\n"
    + "ATOM      4  CA  VAL A   4       0.000   0.000   0.000  "
      "1.00 85.00           C\n"
    + "ATOM      5  CA  PRO A   5       0.000   0.000   0.000  "
      "1.00 83.00           C\n"
    + "END\n"
)
_ZERO_PDB_B64 = base64.b64encode(_ZERO_PDB.encode("utf-8")).decode("ascii")


class TestRunPipelineStubRejection:
    def test_stub_rejection_on_uniform_plddt(self):
        """Silent-stub: every residue pLDDT is identical."""
        from tools.esmfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [0.96] * 50,
            "mean_plddt": 0.96,
            "ptm": None,
            "pdb_b64": _FAKE_PDB_B64,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_nan_plddt(self):
        """Silent-stub: pLDDT contains NaN - weights never loaded."""
        from tools.esmfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [80.0, float("nan"), 85.0, 82.0, 81.0],
            "mean_plddt": 82.0,
            "ptm": None,
            "pdb_b64": _FAKE_PDB_B64,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_implausible_mean_plddt(self):
        """Silent-stub: mean pLDDT outside [0, 100] means units scrambled."""
        from tools.esmfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [500.0, 501.0, 502.0, 503.0, 504.0],
            "mean_plddt": 502.0,
            "ptm": None,
            "pdb_b64": _FAKE_PDB_B64,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_empty_pdb_b64(self):
        """Silent-stub: PDB payload missing or tiny."""
        from tools.esmfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [80.0, 82.0, 85.0, 88.0, 90.0],
            "mean_plddt": 85.0,
            "ptm": None,
            "pdb_b64": "",
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_all_zero_coordinates(self):
        """Degenerate mode: every ATOM has x=y=z=0.0."""
        from tools.esmfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [80.0, 82.0, 85.0, 88.0, 90.0],
            "mean_plddt": 85.0,
            "ptm": None,
            "pdb_b64": _ZERO_PDB_B64,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_accepts_healthy_fold(self):
        """Happy path: real ESMFold output has pLDDT spread >> 1e-6,
        non-NaN, mean in [0, 100], and a real PDB. Must not raise.
        ptm=None is legitimate (ESMFold v1 checkpoints may omit pTM head)."""
        from tools.esmfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [
                85.1, 78.3, 92.5, 88.0, 65.2, 91.8, 72.4, 89.9, 83.5, 76.1,
            ],
            "mean_plddt": 82.28,
            "ptm": None,
            "pdb_b64": _FAKE_PDB_B64,
        }
        rp.reject_stub(parsed)  # must not raise

    def test_stub_rejection_accepts_ptm_zero_on_monomer(self):
        """Unlike ColabFold (D3), ESMFold does NOT reject ptm==0.0 because
        ESMFold v1 checkpoints legitimately omit pTM; zero is just the
        zero-init of a head that isn't wired. pLDDT + PDB are the ground
        truth."""
        from tools.esmfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [
                85.1, 78.3, 92.5, 88.0, 65.2, 91.8, 72.4, 89.9, 83.5, 76.1,
            ],
            "mean_plddt": 82.28,
            "ptm": 0.0,
            "pdb_b64": _FAKE_PDB_B64,
        }
        rp.reject_stub(parsed)  # must not raise


# ---------------------------------------------------------------------------
# Test 8 - shape_output helper round-trips raw ESMFold output to the wire shape
# ---------------------------------------------------------------------------


class TestShapeOutput:
    def test_shape_output_b64s_pdb_and_computes_mean_plddt(self):
        from tools.esmfold import run_pipeline as rp

        raw = {
            "pdb_text": _FAKE_PDB,
            "plddt_per_residue": [85.0, 88.0, 90.0, 91.0, 92.0],
            "ptm": 0.81,
            "pae": None,
        }
        shaped = rp.shape_output(raw)
        assert shaped["pdb_b64"]
        assert base64.b64decode(shaped["pdb_b64"]).startswith(b"HEADER")
        assert shaped["plddt_per_residue"] == [85.0, 88.0, 90.0, 91.0, 92.0]
        assert shaped["mean_plddt"] == pytest.approx(89.2, abs=0.01)
        assert shaped["ptm"] == pytest.approx(0.81)
        assert shaped["pae_matrix_b64"] == ""

    def test_shape_output_packs_pae_when_present(self):
        """If a checkpoint returns pAE, it gets npz-packed as float16."""
        import io

        from tools.esmfold import run_pipeline as rp

        pae = [[1.0, 2.0, 3.0], [2.0, 1.0, 4.0], [3.0, 4.0, 1.0]]
        raw = {
            "pdb_text": _FAKE_PDB,
            "plddt_per_residue": [85.0, 88.0, 90.0],
            "ptm": None,
            "pae": pae,
        }
        shaped = rp.shape_output(raw)
        assert shaped["pae_matrix_b64"]
        raw_bytes = base64.b64decode(shaped["pae_matrix_b64"])
        import numpy as np

        loaded = np.load(io.BytesIO(raw_bytes))
        assert "pae" in loaded
        assert loaded["pae"].shape == (3, 3)

    def test_shape_output_handles_none_ptm_cleanly(self):
        """ptm=None must pass through (ATOMIC-TOOLS.md D4 gotcha - some
        ESMFold checkpoints don't expose pTM, and the results template
        must not crash). The 'None' must survive all the way to the
        final JSON."""
        from tools.esmfold import run_pipeline as rp

        raw = {
            "pdb_text": _FAKE_PDB,
            "plddt_per_residue": [85.0, 88.0, 90.0],
            "ptm": None,
            "pae": None,
        }
        shaped = rp.shape_output(raw)
        assert shaped["ptm"] is None


# ---------------------------------------------------------------------------
# Test 9 - FASTA parser edge cases (tools/esmfold/__init__.py)
# ---------------------------------------------------------------------------


class TestFastaParser:
    def test_parse_bare_sequence(self):
        records, err = esm_mod._parse_fasta_text(UBIQUITIN)
        assert err == ""
        assert len(records) == 1
        assert records[0][1] == UBIQUITIN

    def test_parse_single_record_with_header(self):
        records, err = esm_mod._parse_fasta_text(f">protA\n{UBIQUITIN}")
        assert err == ""
        assert records == [("protA", UBIQUITIN)]

    def test_parse_multi_record(self):
        records, err = esm_mod._parse_fasta_text(
            f">A\n{UBIQUITIN}\n>B\n{UBIQUITIN[:40]}"
        )
        assert err == ""
        assert len(records) == 2
        assert records[0][0] == "A"
        assert records[1][0] == "B"

    def test_parse_seq_lowercase_uppercased(self):
        records, err = esm_mod._parse_fasta_text(
            ">lower\n" + UBIQUITIN.lower()
        )
        assert err == ""
        assert records[0][1] == UBIQUITIN

    def test_parse_empty(self):
        records, err = esm_mod._parse_fasta_text("")
        assert err
        assert records == []


# ---------------------------------------------------------------------------
# Test 10 - Results template exposes PDB download link; omits ptm when None
# ---------------------------------------------------------------------------


def test_results_template_renders_pdb_download_link(app_with_esmfold_flag):
    """Results partial must expose the predicted structure as a
    data-URI download so the user can pull the PDB without a
    server-side export route."""
    from types import SimpleNamespace

    fake_job = SimpleNamespace(
        id="job-esmfold-test",
        tool="esmfold",
        status="succeeded",
        inputs={},
        result={
            "status": "COMPLETED",
            "tier": "standalone",
            "pdb_b64": "SEFMRU9IRUxMTw==",  # valid b64; content irrelevant
            "pae_matrix_b64": "",
            "plddt_per_residue": [85.0, 88.0, 90.0, 91.0, 92.0],
            "mean_plddt": 89.2,
            "ptm": None,
            "chain_count": 1,
            "total_length": 5,
            "runtime_seconds": 30,
        },
    )
    with app_with_esmfold_flag.test_request_context():
        html = app_with_esmfold_flag.jinja_env.get_template(
            "tools/esmfold_results.html"
        ).render(job=fake_job, send_target_tools=None)

    assert 'download="esmfold_job-esmfold-test.pdb"' in html, (
        "PDB download link missing from results partial"
    )
    assert "data:chemical/x-pdb;base64,SEFMRU9IRUxMTw==" in html, (
        "PDB data-URI does not carry the pdb_b64 payload"
    )
    # pae_matrix_b64 is empty - the PAE link must NOT appear.
    assert "pae.npz" not in html, (
        "PAE download link rendered despite empty pae_matrix_b64"
    )


def test_results_template_omits_ptm_tile_when_none(app_with_esmfold_flag):
    """ESMFold v1 checkpoints may omit pTM. The results template must
    not render a broken pTM tile when ptm is None - instead it should
    omit the tile entirely (ATOMIC-TOOLS.md D4 gotcha)."""
    from types import SimpleNamespace

    fake_job = SimpleNamespace(
        id="job-esmfold-noptm",
        tool="esmfold",
        status="succeeded",
        inputs={},
        result={
            "status": "COMPLETED",
            "tier": "standalone",
            "pdb_b64": "U09NRQ==",
            "pae_matrix_b64": "",
            "plddt_per_residue": [85.0, 88.0, 90.0],
            "mean_plddt": 87.7,
            "ptm": None,
            "chain_count": 1,
            "total_length": 3,
            "runtime_seconds": 30,
        },
    )
    with app_with_esmfold_flag.test_request_context():
        html = app_with_esmfold_flag.jinja_env.get_template(
            "tools/esmfold_results.html"
        ).render(job=fake_job, send_target_tools=None)

    # mean pLDDT tile should still render
    assert "mean pLDDT" in html
    # pTM tile should NOT render when ptm is None
    assert "pTM" not in html, (
        "pTM tile rendered despite ptm=None - would show broken/empty metric"
    )
