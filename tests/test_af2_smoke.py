"""Unit tests for the D2 — AF2 standalone atomic tool.

Mirrors ``tests/test_mpnn_smoke.py`` for the D1 pattern setter. Covers
six things per the ATOMIC-TOOLS.md "Definition of Done":

1. The adapter registers with the right slug, presets, credit costs.
2. ``validate()`` accepts well-formed input (FASTA textarea + file) and
   rejects malformed cases (missing preset, empty FASTA, illegal AA,
   length cap).
3. ``build_payload()`` produces the expected job_spec shape the Modal
   pipeline consumes.
4. The Flask form template renders and submit validation rejects
   malformed data (feature flag must be flipped ON in the test process
   so the route is not 404'd).
5. The Modal webhook handler accepts a well-formed COMPLETED POST for
   an AF2 job and rejects replay / unknown-job / bad-token cases.
6. ``run_pipeline.parse_af2_output`` + ``reject_stub`` behave correctly
   on good + bad synthetic outputs.

Runs fully offline — no Modal, no Supabase, no Storage. Uses the same
monkey-patching pattern as ``tests/test_mpnn_smoke.py``.
"""

from __future__ import annotations

import base64
import io
import json
from unittest.mock import patch

import pytest

from tools import af2 as af2_mod
from tools.base import get as get_adapter


# ---------------------------------------------------------------------------
# Test 1 — adapter registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_adapter_registered_under_af2_slug(self):
        adapter = get_adapter("af2")
        assert adapter is not None, "tools.af2 did not register its adapter"
        assert adapter.slug == "af2"

    def test_presets_shape(self):
        adapter = get_adapter("af2")
        slugs = [p.slug for p in adapter.presets]
        assert slugs == ["smoke", "standalone"]

    def test_credit_costs_match_atomic_spec(self):
        """ATOMIC-TOOLS.md D2 + PRODUCT-PLAN.md: smoke=0, standalone=2."""
        adapter = get_adapter("af2")
        smoke = adapter.preset_for("smoke")
        standalone = adapter.preset_for("standalone")
        assert smoke.credits_cost == 0
        assert standalone.credits_cost == 2

    def test_neither_preset_requires_pdb(self):
        """AF2 takes FASTA inline — no PDB upload on any tier."""
        adapter = get_adapter("af2")
        smoke = adapter.preset_for("smoke")
        standalone = adapter.preset_for("standalone")
        assert smoke.requires_pdb is False
        assert standalone.requires_pdb is False

    def test_templates_point_at_af2_partials(self):
        adapter = get_adapter("af2")
        assert adapter.form_template == "tools/af2_form.html"
        assert adapter.results_partial == "tools/af2_results.html"


# ---------------------------------------------------------------------------
# Test 2 — validate() happy path + rejections
# ---------------------------------------------------------------------------


class TestValidate:
    def test_rejects_empty_preset(self):
        inputs, err = af2_mod.validate({}, {})
        assert inputs is None
        assert "preset" in (err or "").lower()

    def test_rejects_unknown_preset(self):
        inputs, err = af2_mod.validate({"preset": "full"}, {})
        assert inputs is None

    def test_smoke_preset_happy_path(self):
        inputs, err = af2_mod.validate({"preset": "smoke"}, {})
        assert err is None
        assert inputs["preset"] == "smoke"
        assert inputs["model_preset"] == "monomer"
        assert inputs["num_recycles"] == 1
        assert inputs["use_templates"] is False
        # Baked BPTI fixture
        assert len(inputs["fasta_records"]) == 1
        assert len(inputs["fasta_records"][0]["sequence"]) == 58

    def test_standalone_happy_path_monomer(self):
        form = {
            "preset": "standalone",
            "fasta": ">test\nMKWVTFISLL\n",
            "num_recycles": "3",
            "use_templates": "on",
        }
        inputs, err = af2_mod.validate(form, {})
        assert err is None, err
        assert inputs["preset"] == "standalone"
        assert inputs["model_preset"] == "monomer"
        assert inputs["num_recycles"] == 3
        assert inputs["use_templates"] is True
        assert inputs["fasta_records"][0]["sequence"] == "MKWVTFISLL"

    def test_standalone_happy_path_multimer(self):
        form = {
            "preset": "standalone",
            "fasta": ">chainA\nMKWVTFI\n>chainB\nSLLFLFSS\n",
            "num_recycles": "2",
        }
        inputs, err = af2_mod.validate(form, {})
        assert err is None, err
        assert inputs["model_preset"] == "multimer"
        assert len(inputs["fasta_records"]) == 2
        assert inputs["fasta_records"][0]["sequence"] == "MKWVTFI"
        assert inputs["fasta_records"][1]["sequence"] == "SLLFLFSS"

    def test_standalone_rejects_empty_fasta(self):
        form = {"preset": "standalone", "fasta": "   "}
        inputs, err = af2_mod.validate(form, {})
        assert inputs is None
        assert err is not None

    def test_standalone_rejects_malformed_fasta_no_header(self):
        form = {"preset": "standalone", "fasta": "MKWVTFISLL\n"}
        inputs, err = af2_mod.validate(form, {})
        assert inputs is None

    def test_standalone_rejects_illegal_amino_acids(self):
        form = {"preset": "standalone", "fasta": ">x\nMKWVT@ISLL\n"}
        inputs, err = af2_mod.validate(form, {})
        assert inputs is None
        assert "illegal" in (err or "").lower() or "standard" in (err or "").lower()

    def test_standalone_rejects_recycles_out_of_range(self):
        base = {"preset": "standalone", "fasta": ">x\nMKWV\n"}
        for bad in ("0", "6", "-1", "99"):
            form = {**base, "num_recycles": bad}
            inputs, err = af2_mod.validate(form, {})
            assert inputs is None, f"num_recycles={bad} should fail"

    def test_standalone_rejects_total_aa_over_cap(self):
        long_seq = "A" * 2000
        form = {"preset": "standalone", "fasta": f">x\n{long_seq}\n"}
        inputs, err = af2_mod.validate(form, {})
        assert inputs is None
        assert "cap" in (err or "").lower() or "1500" in (err or "")

    def test_standalone_rejects_per_chain_cap(self):
        # Two chains both under the total cap but one exceeds per-chain.
        long_seq = "A" * 1450
        short_seq = "A" * 20
        form = {
            "preset": "standalone",
            "fasta": f">x\n{long_seq}\n>y\n{short_seq}\n",
        }
        inputs, err = af2_mod.validate(form, {})
        # Either total or per-chain cap triggers — both are acceptable rejections.
        assert inputs is None

    def test_standalone_accepts_file_upload(self):
        """Uploaded FASTA file (bytes) should work when textarea is empty."""
        class FakeUpload:
            filename = "input.fasta"
            def read(self):
                return b">up1\nMKWVTFISLL\n"

        form = {"preset": "standalone"}
        files = {"fasta_file": FakeUpload()}
        inputs, err = af2_mod.validate(form, files)
        assert err is None, err
        assert inputs["fasta_records"][0]["sequence"] == "MKWVTFISLL"

    def test_use_templates_defaults_true_on_standalone(self):
        """Spec default is use_templates=True. Form with key absent should keep it True."""
        form = {"preset": "standalone", "fasta": ">x\nMKWV\n"}
        inputs, err = af2_mod.validate(form, {})
        assert err is None
        assert inputs["use_templates"] is True


# ---------------------------------------------------------------------------
# Test 3 — build_payload() shape
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_smoke_payload_shape(self):
        inputs, _ = af2_mod.validate({"preset": "smoke"}, {})
        payload = af2_mod.build_payload(inputs, presigned_url="")
        assert "fasta_records" in payload
        assert payload["parameters"]["model_preset"] == "monomer"
        assert payload["parameters"]["num_recycles"] == 1
        assert payload["parameters"]["use_templates"] is False

    def test_standalone_payload_shape(self):
        inputs, _ = af2_mod.validate(
            {
                "preset": "standalone",
                "fasta": ">a\nMKWV\n>b\nSLLF\n",
                "num_recycles": "4",
                "use_templates": "on",
            },
            {},
        )
        payload = af2_mod.build_payload(inputs, presigned_url="https://x")
        assert len(payload["fasta_records"]) == 2
        assert payload["parameters"]["model_preset"] == "multimer"
        assert payload["parameters"]["num_recycles"] == 4
        assert payload["parameters"]["use_templates"] is True
        # Presigned URL is ignored by AF2 — not embedded.
        assert "presigned" not in json.dumps(payload).lower()


# ---------------------------------------------------------------------------
# Test 4 — Flask form + submit validation
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_af2_flag(monkeypatch):
    """Boot the tools-hub Flask app with FLAG_TOOL_AF2=on so the route
    resolves rather than 404s."""
    monkeypatch.setenv("FLAG_TOOL_AF2", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login_session(client, email="user@example.com"):
    """Set session cookie so ``@login_required`` routes pass."""
    with client.session_transaction() as sess:
        sess["user_email"] = email


def test_form_renders_when_flag_on(app_with_af2_flag, monkeypatch):
    """GET /tools/af2 renders the form when the flag is flipped on."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_af2_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/af2")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "AlphaFold" in body
    assert "Smoke" in body
    assert "Standalone" in body


def test_form_404s_when_flag_off(app_with_af2_flag, monkeypatch):
    """With the flag removed, the route must 404 — launch-gate contract."""
    monkeypatch.delenv("FLAG_TOOL_AF2", raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_af2_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/af2")
    assert resp.status_code == 404


def test_submit_rejects_unknown_preset(app_with_af2_flag, monkeypatch):
    """POST with a bad preset rerenders the form with the validation error."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_af2_flag.test_client()
    _login_session(client)
    resp = client.post(
        "/tools/af2/submit",
        data={"preset": "bogus"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "preset" in body.lower()


def test_handoff_pilot_preset_maps_to_standalone_not_smoke(
    app_with_af2_flag, monkeypatch
):
    """Cross-tool ``from_job`` handoff sets pre_fill['preset']='pilot'.
    AF2 has no 'pilot' option. Template must remap to 'standalone' so
    the user's target is actually used — otherwise ``loop.first``
    silently selects 'smoke', which runs the baked BPTI fixture and
    burns credits on the wrong sequence (mirrors the D1 MPNN Codex P1)."""
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
        tool="mpnn",
        inputs={
            "target_chain": "A",
        },
    )
    monkeypatch.setattr(
        "app.get_job",
        lambda job_id, user_id: mock_src if job_id == "src-job-abc" else None,
    )
    client = app_with_af2_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/af2?from_job=src-job-abc")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    smoke_opt = re.search(r'<option value="smoke"[^>]*>', body)
    standalone_opt = re.search(r'<option value="standalone"[^>]*>', body)
    assert smoke_opt and standalone_opt, "expected both preset options rendered"
    assert "selected" in standalone_opt.group(0), (
        "pilot->standalone remap failed: standalone option was not selected"
    )
    assert "selected" not in smoke_opt.group(0), (
        "smoke option should not be selected on a handoff"
    )


def test_af2_download_pdb_route(app_with_af2_flag, monkeypatch):
    """/jobs/<id>/af2.pdb returns the base64-decoded PDB bytes."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    fake_pdb = b"ATOM      1  N   MET A   1      10.000  10.000  10.000  1.00 90.00           N\n"
    job = SimpleNamespace(
        id="af2-job-1",
        tool="af2",
        status="succeeded",
        inputs={},
        result={"pdb_b64": base64.b64encode(fake_pdb).decode("ascii")},
    )
    monkeypatch.setattr(
        "app.get_job",
        lambda job_id, user_id: job if job_id == "af2-job-1" else None,
    )
    client = app_with_af2_flag.test_client()
    _login_session(client)
    resp = client.get("/jobs/af2-job-1/af2.pdb")
    assert resp.status_code == 200
    assert b"ATOM" in resp.get_data()


def test_af2_download_pae_route(app_with_af2_flag, monkeypatch):
    """/jobs/<id>/af2_pae.npy returns the base64-decoded .npy bytes."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    # Build a real .npy buffer
    try:
        import numpy as np
    except ImportError:
        pytest.skip("numpy not available")
    buf = io.BytesIO()
    np.save(buf, np.ones((4, 4), dtype=np.float32))
    npy_bytes = buf.getvalue()
    job = SimpleNamespace(
        id="af2-job-2",
        tool="af2",
        status="succeeded",
        inputs={},
        result={"pae_matrix_b64": base64.b64encode(npy_bytes).decode("ascii")},
    )
    monkeypatch.setattr(
        "app.get_job",
        lambda job_id, user_id: job if job_id == "af2-job-2" else None,
    )
    client = app_with_af2_flag.test_client()
    _login_session(client)
    resp = client.get("/jobs/af2-job-2/af2_pae.npy")
    assert resp.status_code == 200
    # Can round-trip via numpy.load
    loaded = np.load(io.BytesIO(resp.get_data()))
    assert loaded.shape == (4, 4)


def test_af2_download_rejects_non_af2_job(app_with_af2_flag, monkeypatch):
    """PDB download route must 404 for non-af2 jobs (owner-scoping +
    tool-scoping). Prevents /jobs/<bindcraft-job-id>/af2.pdb from
    returning garbage."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    job = SimpleNamespace(
        id="bc-job-1",
        tool="bindcraft",
        status="succeeded",
        inputs={},
        result={"candidates": []},
    )
    monkeypatch.setattr(
        "app.get_job",
        lambda job_id, user_id: job if job_id == "bc-job-1" else None,
    )
    client = app_with_af2_flag.test_client()
    _login_session(client)
    resp = client.get("/jobs/bc-job-1/af2.pdb")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 5 — Modal webhook handler accepts/rejects correctly
# ---------------------------------------------------------------------------


class TestWebhookRoundtrip:
    """Exercise the shared webhook handler against an AF2 job."""

    def _fake_job(self, status="running", token="t" * 64, tool="af2"):
        from types import SimpleNamespace
        return SimpleNamespace(
            id="job-uuid-af2",
            job_token=token,
            status=status,
            tool=tool,
        )

    def test_rejects_unknown_job(self, app_with_af2_flag, monkeypatch):
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: None)
        client = app_with_af2_flag.test_client()
        resp = client.post(
            "/webhooks/modal/missing-af2-job/some-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 404

    def test_rejects_bad_token(self, app_with_af2_flag, monkeypatch):
        fake = self._fake_job(token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_af2_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/wrong-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 403

    def test_accepts_completed_with_good_token(
        self, app_with_af2_flag, monkeypatch
    ):
        fake = self._fake_job(status="running", token="good-token")
        fresh = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        monkeypatch.setattr(
            "webhooks.modal.complete_job",
            lambda *a, **kw: fresh,
        )
        client = app_with_af2_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={
                "status": "COMPLETED",
                "output": {
                    "pdb_b64": base64.b64encode(b"ATOM").decode("ascii"),
                    "plddt_per_residue": [85.0, 90.0, 88.0],
                    "ptm": 0.82,
                    "runtime_seconds": 420,
                },
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "recorded"

    def test_replay_on_terminal_is_noop(
        self, app_with_af2_flag, monkeypatch
    ):
        """Replaying a POST on a terminal job must not mutate state."""
        fake = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_af2_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "already_terminal"


# ---------------------------------------------------------------------------
# Test 6 — Modal payload + app-name override
# ---------------------------------------------------------------------------


class TestSmokePresetShape:
    def test_app_name_override_maps_af2_to_ranomics_namespace(self):
        """Sanity: slug "af2" resolves to ``ranomics-af2-prod``."""
        from gpu.modal_client import modal_app_name

        assert modal_app_name("af2") == "ranomics-af2-prod"
        # Non-overridden tools keep the kendrew- prefix.
        assert modal_app_name("bindcraft") == "kendrew-bindcraft-prod"

    def test_preset_gpu_seconds_caps_registered(self):
        """Both AF2 presets have an entry in PRESET_CAPS."""
        from gpu.modal_client import preset_gpu_seconds

        assert preset_gpu_seconds("af2", "smoke") == 180
        assert preset_gpu_seconds("af2", "standalone") == 1200

    def test_modal_payload_for_smoke_offline_stub(self, monkeypatch):
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = af2_mod.validate({"preset": "smoke"}, {})
        payload = af2_mod.build_payload(inputs, presigned_url="")
        result = client.submit(
            "af2",
            "smoke",
            inputs={**payload, "_input_presigned_url": ""},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="",
        )
        assert result["function_call_id"].startswith("fc-stub-af2-smoke-")
        assert result["gpu_seconds_cap"] == 180

    def test_modal_payload_for_standalone_offline_stub(self, monkeypatch):
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = af2_mod.validate(
            {
                "preset": "standalone",
                "fasta": ">x\nMKWVTFISLL\n",
                "num_recycles": "3",
            },
            {},
        )
        payload = af2_mod.build_payload(inputs, presigned_url="")
        result = client.submit(
            "af2",
            "standalone",
            inputs={**payload, "_input_presigned_url": ""},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="https://tools/webhook",
        )
        assert result["function_call_id"].startswith("fc-stub-af2-standalone-")
        assert result["gpu_seconds_cap"] == 1200


# ---------------------------------------------------------------------------
# Test 7 — run_pipeline.py parser + stub rejection
# ---------------------------------------------------------------------------
#
# Full main() requires a GPU + colabfold_batch binary, so we exercise
# only the parser + stub-rejection logic here. The rest is covered by
# the live smoke validation the user owes on Modal.


class TestRunPipelineParser:
    def _write_colabfold_outputs(self, out_dir, *, plddt, pae, ptm, iptm=None):
        """Write a minimal ColabFold-style output tree."""
        pdb = (
            "HEADER    FAKE AF2 OUTPUT\n"
            "ATOM      1  N   MET A   1      10.000  10.000  10.000  1.00 90.00           N\n"
            "ATOM      2  CA  MET A   1      10.500  10.500  10.500  1.00 90.00           C\n"
            "END\n"
        )
        pdb_path = out_dir / "job_unrelaxed_rank_001_AlphaFold2-ptm_model_1_seed_000.pdb"
        pdb_path.write_text(pdb)
        scores = {
            "plddt": plddt,
            "pae": pae,
            "ptm": ptm,
        }
        if iptm is not None:
            scores["iptm"] = iptm
        scores_path = (
            out_dir
            / "job_scores_rank_001_AlphaFold2-ptm_model_1_seed_000.json"
        )
        scores_path.write_text(json.dumps(scores))

    def test_parser_extracts_plddt_ptm_and_pae(self, tmp_path):
        """Happy path: real-shaped ColabFold output parses into the schema."""
        from tools.af2 import run_pipeline as rp

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        fasta = tmp_path / "in.fasta"
        fasta.write_text(">x\nMKWVT\n")
        plddt = [85.0, 90.0, 88.0, 92.0, 87.0]
        pae = [[0.0, 1.0, 1.5, 2.0, 2.5], [1.0, 0.0, 1.2, 1.8, 2.2],
               [1.5, 1.2, 0.0, 1.0, 1.8], [2.0, 1.8, 1.0, 0.0, 1.2],
               [2.5, 2.2, 1.8, 1.2, 0.0]]
        self._write_colabfold_outputs(
            out_dir, plddt=plddt, pae=pae, ptm=0.82
        )
        parsed = rp.parse_af2_output(out_dir, fasta=fasta)
        assert parsed["plddt_per_residue"] == [85.0, 90.0, 88.0, 92.0, 87.0]
        assert parsed["ptm"] == pytest.approx(0.82)
        assert parsed["iptm"] is None
        assert parsed["pae_shape"] == [5, 5]
        # PDB base64 round-trips to PDB text
        import base64 as _b64
        pdb_text = _b64.b64decode(parsed["pdb_b64"]).decode("ascii")
        assert "ATOM" in pdb_text
        # PAE .npy round-trips via numpy.load
        import numpy as _np
        pae_bytes = _b64.b64decode(parsed["pae_matrix_b64"])
        arr = _np.load(io.BytesIO(pae_bytes))
        assert arr.shape == (5, 5)

    def test_parser_carries_iptm_on_multimer(self, tmp_path):
        from tools.af2 import run_pipeline as rp

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        fasta = tmp_path / "in.fasta"
        fasta.write_text(">x\nMKWV:TFISLL\n")
        plddt = [85.0, 90.0, 88.0, 92.0, 87.0, 91.0, 84.0, 89.0, 86.0, 93.0]
        pae = [[0.0] * 10 for _ in range(10)]
        # Inject variation so the PAE isn't a degenerate matrix.
        for i in range(10):
            for j in range(10):
                pae[i][j] = abs(i - j) * 0.5
        self._write_colabfold_outputs(
            out_dir, plddt=plddt, pae=pae, ptm=0.78, iptm=0.65
        )
        parsed = rp.parse_af2_output(out_dir, fasta=fasta)
        assert parsed["iptm"] == pytest.approx(0.65)
        assert parsed["ptm"] == pytest.approx(0.78)
        # Multimer shape detected from the ":"-joined seq in the FASTA
        assert parsed["num_chains"] == 2

    def test_stub_rejection_on_all_identical_plddt(self):
        """Silent-stub failure: every pLDDT value is identical."""
        from tools.af2 import run_pipeline as rp

        result = {
            "plddt_per_residue": [50.0] * 30,
            "ptm": 0.5,
            "iptm": None,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(result)

    def test_stub_rejection_on_nan_plddt(self):
        """Numerical blow-up / cuDNN mismatch failure mode."""
        from tools.af2 import run_pipeline as rp

        result = {
            "plddt_per_residue": [float("nan")] * 10,
            "ptm": 0.5,
            "iptm": None,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(result)

    def test_stub_rejection_on_baseline_zero_plddt(self):
        """All-zero or near-zero pLDDT (baseline garbage)."""
        from tools.af2 import run_pipeline as rp

        result = {
            "plddt_per_residue": [0.0, 0.1, 0.05, 0.2, 0.15],
            "ptm": 0.1,
            "iptm": None,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(result)

    def test_stub_rejection_on_both_ptm_iptm_zero(self):
        """Degenerate head outputs — pTM + ipTM both zero."""
        from tools.af2 import run_pipeline as rp

        result = {
            "plddt_per_residue": [85.0, 70.0, 90.0, 60.0, 80.0],
            "ptm": 0.0,
            "iptm": 0.0,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(result)

    def test_stub_rejection_accepts_healthy_output(self):
        """Happy path: real AF2 output has spread + nonzero pTM."""
        from tools.af2 import run_pipeline as rp

        result = {
            "plddt_per_residue": [65.0, 80.0, 92.0, 88.0, 70.0, 95.0, 72.0],
            "ptm": 0.78,
            "iptm": None,
        }
        rp.reject_stub(result)  # Must not raise.

    def test_stub_rejection_accepts_multimer_healthy(self):
        """Multimer with healthy ipTM."""
        from tools.af2 import run_pipeline as rp

        result = {
            "plddt_per_residue": [65.0, 80.0, 92.0, 88.0, 70.0, 95.0, 72.0],
            "ptm": 0.72,
            "iptm": 0.64,
        }
        rp.reject_stub(result)
