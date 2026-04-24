"""Unit tests for the D3 — ColabFold standalone atomic tool.

Mirrors ``tests/test_mpnn_smoke.py`` end-to-end:

1. The adapter registers with the right slug, presets, credit costs.
2. ``validate()`` accepts well-formed input and rejects every known
   malformed case (missing FASTA, bad residues, oversized sequences).
3. ``build_payload()`` produces the expected Kendrew job_spec shape
   with ``fasta_text`` delivered inline (no file upload).
4. The Flask form template renders and submit validation rejects
   malformed data (feature flag must be flipped ON in the test process
   so the route is not 404'd).
5. The Modal webhook handler accepts a well-formed COMPLETED POST for
   a ColabFold job and rejects replay / unknown-job / bad-token cases.
6. The parser extracts pLDDT / ptm / iptm correctly and the
   stub-rejection guard trips on every known silent-stub signature.

Runs fully offline — no Modal, no Supabase, no GPU. Uses the same
monkey-patching pattern as ``tests/test_mpnn_smoke.py``.
"""

from __future__ import annotations

import base64
import io
import json
from unittest.mock import patch

import pytest

from tools import colabfold as cf_mod
from tools.base import get as get_adapter


# ---------------------------------------------------------------------------
# Test 1 — adapter registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_adapter_registered_under_colabfold_slug(self):
        adapter = get_adapter("colabfold")
        assert adapter is not None, "tools.colabfold did not register its adapter"
        assert adapter.slug == "colabfold"

    def test_presets_shape(self):
        adapter = get_adapter("colabfold")
        slugs = [p.slug for p in adapter.presets]
        assert slugs == ["smoke", "standalone"]

    def test_credit_costs_match_atomic_spec(self):
        """ATOMIC-TOOLS.md D3 + PRODUCT-PLAN.md: smoke=0, standalone=2."""
        adapter = get_adapter("colabfold")
        smoke = adapter.preset_for("smoke")
        standalone = adapter.preset_for("standalone")
        assert smoke.credits_cost == 0
        assert standalone.credits_cost == 2

    def test_neither_preset_requires_pdb(self):
        """ColabFold takes FASTA text, never a PDB upload."""
        adapter = get_adapter("colabfold")
        assert adapter.requires_pdb is False
        for p in adapter.presets:
            assert p.requires_pdb is False

    def test_templates_point_at_colabfold_partials(self):
        adapter = get_adapter("colabfold")
        assert adapter.form_template == "tools/colabfold_form.html"
        assert adapter.results_partial == "tools/colabfold_results.html"


# ---------------------------------------------------------------------------
# Test 2 — validate() happy path + rejections
# ---------------------------------------------------------------------------


UBIQUITIN = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"


class TestValidate:
    def test_rejects_empty_preset(self):
        inputs, err = cf_mod.validate({}, {})
        assert inputs is None
        assert "preset" in (err or "").lower()

    def test_rejects_unknown_preset(self):
        inputs, err = cf_mod.validate({"preset": "full"}, {})
        assert inputs is None

    def test_smoke_preset_happy_path(self):
        inputs, err = cf_mod.validate({"preset": "smoke"}, {})
        assert err is None
        assert inputs["preset"] == "smoke"
        assert inputs["fasta_text"] == ""
        assert inputs["num_recycles"] == 1
        assert inputs["use_templates"] is False

    def test_standalone_happy_path_with_header(self):
        form = {
            "preset": "standalone",
            "fasta_text": f">ubiquitin\n{UBIQUITIN}",
            "num_recycles": "1",
        }
        inputs, err = cf_mod.validate(form, {})
        assert err is None, err
        assert inputs["preset"] == "standalone"
        assert ">ubiquitin" in inputs["fasta_text"]
        assert UBIQUITIN in inputs["fasta_text"]
        assert inputs["num_recycles"] == 1

    def test_standalone_accepts_bare_sequence(self):
        """Bare sequence (no >header) gets normalised to include a header."""
        form = {"preset": "standalone", "fasta_text": UBIQUITIN}
        inputs, err = cf_mod.validate(form, {})
        assert err is None, err
        assert inputs["fasta_text"].startswith(">")
        assert UBIQUITIN in inputs["fasta_text"]

    def test_standalone_rejects_empty_fasta(self):
        form = {"preset": "standalone", "fasta_text": "   "}
        inputs, err = cf_mod.validate(form, {})
        assert inputs is None
        assert err is not None

    def test_standalone_rejects_non_canonical_residues(self):
        """B, J, O, U, Z, * are not in the canonical 20 + X alphabet."""
        form = {
            "preset": "standalone",
            "fasta_text": f">bad\n{UBIQUITIN[:40]}BJOZ{UBIQUITIN[40:]}",
        }
        inputs, err = cf_mod.validate(form, {})
        assert inputs is None
        assert "non-canonical" in (err or "")

    def test_standalone_rejects_oversized_sequence(self):
        """> 600 aa must be rejected at form validation time."""
        giant = "A" * 700
        form = {"preset": "standalone", "fasta_text": f">big\n{giant}"}
        inputs, err = cf_mod.validate(form, {})
        assert inputs is None
        assert "max" in (err or "").lower()

    def test_standalone_rejects_tiny_sequence(self):
        form = {"preset": "standalone", "fasta_text": ">tiny\nAAAA"}
        inputs, err = cf_mod.validate(form, {})
        assert inputs is None

    def test_standalone_rejects_recycles_out_of_range(self):
        for bad in ("0", "10", "-1"):
            form = {
                "preset": "standalone",
                "fasta_text": f">x\n{UBIQUITIN}",
                "num_recycles": bad,
            }
            inputs, err = cf_mod.validate(form, {})
            assert inputs is None, f"recycles={bad} should fail"

    def test_standalone_multimer_under_cap_ok(self):
        """Two chains totalling <=600 aa must pass AND be normalised into
        the single-record ``A:B`` form ColabFold expects for complexes.
        Codex P1: two ``>`` records would silently launch as two monomer
        jobs instead of a complex."""
        chain_a = "A" * 100
        chain_b = "L" * 100
        fasta = f">chainA\n{chain_a}\n>chainB\n{chain_b}"
        form = {"preset": "standalone", "fasta_text": fasta}
        inputs, err = cf_mod.validate(form, {})
        assert err is None, err
        # Exactly one FASTA record, with a ``:`` chain separator.
        assert inputs["fasta_text"].count(">") == 1, inputs["fasta_text"]
        assert ":" in inputs["fasta_text"], (
            "multimer must use ':' chain separator for colabfold_batch"
        )
        assert chain_a in inputs["fasta_text"]
        assert chain_b in inputs["fasta_text"]

    def test_standalone_monomer_single_record(self):
        """Single-chain input stays as exactly one FASTA record, no ``:``."""
        form = {"preset": "standalone", "fasta_text": f">protA\n{UBIQUITIN}"}
        inputs, err = cf_mod.validate(form, {})
        assert err is None, err
        assert inputs["fasta_text"].count(">") == 1
        assert ":" not in inputs["fasta_text"]

    def test_standalone_multimer_over_total_cap_rejected(self):
        """Two 400 aa chains (800 total) blow the 600 aa total cap."""
        chain = "A" * 400
        fasta = f">chainA\n{chain}\n>chainB\n{chain}"
        form = {"preset": "standalone", "fasta_text": fasta}
        inputs, err = cf_mod.validate(form, {})
        # Per-chain cap already trips at 400 (< 600), so the per-chain
        # check isn't what fires. But each 400 aa chain is individually
        # legal and the total is 800 aa, which must trip the total cap.
        # Individual chains at 400 are under SEQ_LEN_MAX=600, so this
        # reaches the total-length guard.
        assert inputs is None
        assert "total" in (err or "").lower() or "max" in (err or "").lower()

    def test_standalone_use_templates_checkbox_parsed(self):
        for raw, expected in [("on", True), ("", False), (None, False)]:
            form = {
                "preset": "standalone",
                "fasta_text": f">x\n{UBIQUITIN}",
            }
            if raw is not None:
                form["use_templates"] = raw
            inputs, err = cf_mod.validate(form, {})
            assert err is None, (raw, err)
            assert inputs["use_templates"] is expected, (raw, inputs)


# ---------------------------------------------------------------------------
# Test 3 — build_payload() shape
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_smoke_payload_shape(self):
        inputs, _ = cf_mod.validate({"preset": "smoke"}, {})
        payload = cf_mod.build_payload(inputs, presigned_url="")
        assert payload["fasta_text"] == ""
        assert payload["parameters"]["num_recycles"] == 1
        assert payload["parameters"]["use_templates"] is False

    def test_standalone_payload_shape(self):
        inputs, _ = cf_mod.validate(
            {
                "preset": "standalone",
                "fasta_text": f">x\n{UBIQUITIN}",
                "num_recycles": "3",
                "use_templates": "on",
            },
            {},
        )
        payload = cf_mod.build_payload(inputs, presigned_url="https://ignored")
        assert payload["fasta_text"].startswith(">")
        assert UBIQUITIN in payload["fasta_text"]
        assert payload["parameters"]["num_recycles"] == 3
        assert payload["parameters"]["use_templates"] is True
        # FASTA travels inline — no presigned URL embedded.
        assert "https://ignored" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Test 4 — Flask form + submit validation
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_colabfold_flag(monkeypatch):
    """Boot the tools-hub Flask app with FLAG_TOOL_COLABFOLD=on so the
    route resolves rather than 404s."""
    monkeypatch.setenv("FLAG_TOOL_COLABFOLD", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login_session(client, email="user@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def test_form_renders_when_flag_on(app_with_colabfold_flag, monkeypatch):
    """GET /tools/colabfold renders the form when the flag is flipped on."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_colabfold_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/colabfold")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ColabFold" in body
    assert "Smoke" in body
    assert "Standalone" in body


def test_form_404s_when_flag_off(app_with_colabfold_flag, monkeypatch):
    """With the flag removed, the route must 404 — launch-gate contract."""
    monkeypatch.delenv("FLAG_TOOL_COLABFOLD", raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_colabfold_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/colabfold")
    assert resp.status_code == 404


def test_submit_rejects_unknown_preset(app_with_colabfold_flag, monkeypatch):
    """POST with a bad preset rerenders the form with the validation error."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_colabfold_flag.test_client()
    _login_session(client)
    resp = client.post(
        "/tools/colabfold/submit",
        data={"preset": "bogus"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "preset" in body.lower()


def test_handoff_pilot_preset_maps_to_standalone_not_smoke(
    app_with_colabfold_flag, monkeypatch
):
    """Cross-tool ``from_job`` handoff sets pre_fill['preset']='pilot'.
    ColabFold has no 'pilot' option. Template must remap to 'standalone'
    so the incoming sequence is actually used — otherwise ``loop.first``
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
    client = app_with_colabfold_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/colabfold?from_job=src-job-abc")
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
# Test 5 — Modal webhook handler accepts/rejects correctly
# ---------------------------------------------------------------------------


class TestWebhookRoundtrip:
    """Exercise the shared webhook handler against a ColabFold job. We do
    not test the full complete_job pipeline (Supabase dependency); we
    test the handler's response to each auth state and to each payload
    status."""

    def _fake_job(self, status="running", token="t" * 64, tool="colabfold"):
        from types import SimpleNamespace
        return SimpleNamespace(
            id="job-uuid-1",
            job_token=token,
            status=status,
            tool=tool,
        )

    def test_rejects_unknown_job(self, app_with_colabfold_flag, monkeypatch):
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: None)
        client = app_with_colabfold_flag.test_client()
        resp = client.post(
            "/webhooks/modal/missing-job/some-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 404

    def test_rejects_bad_token(self, app_with_colabfold_flag, monkeypatch):
        fake = self._fake_job(token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_colabfold_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/wrong-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 403

    def test_accepts_completed_with_good_token(
        self, app_with_colabfold_flag, monkeypatch
    ):
        fake = self._fake_job(status="running", token="good-token")
        fresh = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        monkeypatch.setattr(
            "webhooks.modal.complete_job", lambda *a, **kw: fresh
        )
        client = app_with_colabfold_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={
                "status": "COMPLETED",
                "output": {
                    "pdb_b64": "TU9DS19QREI=",
                    "plddt_per_residue": [80.0, 82.5, 81.0],
                    "mean_plddt": 81.17,
                    "ptm": 0.79,
                    "runtime_seconds": 65,
                },
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "recorded"

    def test_replay_on_terminal_is_noop(
        self, app_with_colabfold_flag, monkeypatch
    ):
        """Replaying the same POST after the job is already terminal
        must not mutate state — returns ``already_terminal``."""
        fake = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_colabfold_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "already_terminal"


# ---------------------------------------------------------------------------
# Test 6 — modal_client PRESET_CAPS + APP_NAME_OVERRIDES wiring
# ---------------------------------------------------------------------------


class TestSmokePresetShape:
    def test_app_name_override_maps_colabfold_to_ranomics_namespace(self):
        """Sanity: slug "colabfold" resolves to ``ranomics-colabfold-prod``."""
        from gpu.modal_client import modal_app_name

        assert modal_app_name("colabfold") == "ranomics-colabfold-prod"
        # Non-overridden tools keep the kendrew- prefix.
        assert modal_app_name("bindcraft") == "kendrew-bindcraft-prod"

    def test_preset_gpu_seconds_caps_registered(self):
        """Both ColabFold presets have an entry in PRESET_CAPS — the
        generic submit route raises ``ValueError`` otherwise."""
        from gpu.modal_client import preset_gpu_seconds

        assert preset_gpu_seconds("colabfold", "smoke") == 120
        assert preset_gpu_seconds("colabfold", "standalone") == 420

    def test_modal_payload_for_smoke_offline_stub(self, monkeypatch):
        """With modal patched to None, submit returns the deterministic stub."""
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = cf_mod.validate({"preset": "smoke"}, {})
        payload = cf_mod.build_payload(inputs, presigned_url="")
        result = client.submit(
            "colabfold",
            "smoke",
            inputs={**payload, "_input_presigned_url": ""},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="",
        )
        assert result["function_call_id"].startswith(
            "fc-stub-colabfold-smoke-"
        )
        assert result["gpu_seconds_cap"] == 120

    def test_modal_payload_for_standalone_offline_stub(self, monkeypatch):
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = cf_mod.validate(
            {
                "preset": "standalone",
                "fasta_text": f">x\n{UBIQUITIN}",
                "num_recycles": "1",
            },
            {},
        )
        payload = cf_mod.build_payload(inputs, presigned_url="")
        result = client.submit(
            "colabfold",
            "standalone",
            inputs={**payload, "_input_presigned_url": ""},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="https://tools/webhook",
        )
        assert result["function_call_id"].startswith(
            "fc-stub-colabfold-standalone-"
        )
        assert result["gpu_seconds_cap"] == 420


# ---------------------------------------------------------------------------
# Test 7 — run_pipeline.py parser + stub rejection
# ---------------------------------------------------------------------------


class TestRunPipelineParser:
    def _write_colabfold_output(
        self,
        tmp_path,
        *,
        plddt,
        ptm=0.79,
        iptm=None,
        pae=None,
    ):
        """Write a fake colabfold_batch output directory with a rank-001
        scores JSON + a matching unrelaxed PDB (dummy content)."""
        rank_tag = "rank_001_alphafold2_multimer_v3_model_1_seed_000"
        scores_name = f"query_scores_{rank_tag}.json"
        pdb_name = f"query_unrelaxed_{rank_tag}.pdb"

        scores = {"plddt": plddt, "ptm": ptm}
        if iptm is not None:
            scores["iptm"] = iptm
        if pae is not None:
            scores["pae"] = pae

        (tmp_path / scores_name).write_text(json.dumps(scores))
        # Dummy PDB: well over the 200-byte tiny-reject threshold.
        dummy_pdb = (
            "HEADER    TEST PDB                                "
            "20-APR-26   XXXX\n"
            + "ATOM      1  CA  ALA A   1      10.000  20.000  30.000  "
              "1.00 80.00           C\n" * 5
            + "END\n"
        )
        (tmp_path / pdb_name).write_text(dummy_pdb)
        return tmp_path

    def test_parser_extracts_plddt_ptm_iptm(self, tmp_path):
        """Rank-001 scores JSON + PDB → schema matches D2 AF2 output shape."""
        from tools.colabfold import run_pipeline as rp

        plddt = [85.1, 88.3, 90.5, 91.0, 92.2, 89.8]
        self._write_colabfold_output(tmp_path, plddt=plddt, ptm=0.83, iptm=0.76)
        parsed = rp.parse_colabfold_output(tmp_path)
        assert parsed["plddt_per_residue"] == [
            pytest.approx(x) for x in plddt
        ]
        assert parsed["ptm"] == pytest.approx(0.83)
        assert parsed["iptm"] == pytest.approx(0.76)
        assert parsed["mean_plddt"] == pytest.approx(
            sum(plddt) / len(plddt), abs=0.01
        )
        # PDB b64 round-trips to >200 bytes.
        raw = base64.b64decode(parsed["pdb_b64"])
        assert len(raw) > 200
        assert b"ATOM" in raw

    def test_parser_handles_monomer_without_iptm(self, tmp_path):
        """Monomer AF2 output omits iptm — parser returns None."""
        from tools.colabfold import run_pipeline as rp

        self._write_colabfold_output(
            tmp_path, plddt=[80.0] * 10 + [82.0] * 10, ptm=0.70, iptm=None
        )
        parsed = rp.parse_colabfold_output(tmp_path)
        assert parsed["iptm"] is None
        assert parsed["ptm"] == pytest.approx(0.70)

    def test_parser_packs_pae_matrix_as_npz_b64(self, tmp_path):
        """PAE matrix is b64-packed as npz-compressed float16."""
        from tools.colabfold import run_pipeline as rp

        pae = [[1.0, 2.0, 3.0], [2.0, 1.0, 4.0], [3.0, 4.0, 1.0]]
        self._write_colabfold_output(
            tmp_path, plddt=[80.0, 82.0, 84.0], ptm=0.75, pae=pae
        )
        parsed = rp.parse_colabfold_output(tmp_path)
        assert parsed["pae_matrix_b64"]
        # Round-trip: b64 -> npz -> array
        raw = base64.b64decode(parsed["pae_matrix_b64"])
        import numpy as np
        loaded = np.load(io.BytesIO(raw))
        assert "pae" in loaded
        assert loaded["pae"].shape == (3, 3)

    def test_stub_rejection_on_uniform_plddt(self):
        """Silent-stub: every residue pLDDT is identical."""
        from tools.colabfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [0.96] * 50,
            "mean_plddt": 0.96,
            "iptm": None,
            "ptm": 0.5,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_nan_plddt(self):
        """Silent-stub: pLDDT contains NaN — weights never loaded."""
        from tools.colabfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [80.0, float("nan"), 85.0, 82.0, 81.0],
            "mean_plddt": 82.0,
            "iptm": None,
            "ptm": 0.7,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_zero_iptm(self):
        """Silent-stub: iptm exactly 0.0 — multimer head bypassed."""
        from tools.colabfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [85.0, 82.0, 90.0, 88.0, 91.0],
            "mean_plddt": 87.2,
            "iptm": 0.0,
            "ptm": 0.6,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_zero_ptm_monomer(self):
        """Silent-stub: ptm exactly 0.0 on a monomer (no iptm reported) —
        Codex P2: original guard only checked iptm so monomer stubs
        silently succeeded."""
        from tools.colabfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [85.0, 82.0, 90.0, 88.0, 91.0],
            "mean_plddt": 87.2,
            "iptm": None,
            "ptm": 0.0,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_on_implausible_mean_plddt(self):
        """Silent-stub: mean pLDDT outside [0, 100] means units scrambled."""
        from tools.colabfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [500.0] * 20,
            "mean_plddt": 500.0,
            "iptm": None,
            "ptm": 0.7,
        }
        with pytest.raises(SystemExit):
            rp.reject_stub(parsed)

    def test_stub_rejection_accepts_healthy_fold(self):
        """Happy path: real AF2 output has pLDDT spread >> 1e-6, non-NaN,
        pTM in [0,1], and mean pLDDT in [0, 100]. Must not raise."""
        from tools.colabfold import run_pipeline as rp

        parsed = {
            "plddt_per_residue": [
                85.1, 78.3, 92.5, 88.0, 65.2, 91.8, 72.4, 89.9, 83.5, 76.1,
            ],
            "mean_plddt": 82.28,
            "iptm": 0.76,
            "ptm": 0.81,
        }
        rp.reject_stub(parsed)  # must not raise


# ---------------------------------------------------------------------------
# Test 8 — FASTA parser edge cases (tools/colabfold/__init__.py)
# ---------------------------------------------------------------------------


class TestFastaParser:
    def test_parse_bare_sequence(self):
        records, err = cf_mod._parse_fasta_text(UBIQUITIN)
        assert err == ""
        assert len(records) == 1
        assert records[0][1] == UBIQUITIN

    def test_parse_single_record_with_header(self):
        records, err = cf_mod._parse_fasta_text(f">protA\n{UBIQUITIN}")
        assert err == ""
        assert records == [("protA", UBIQUITIN)]

    def test_parse_multi_record(self):
        records, err = cf_mod._parse_fasta_text(
            f">A\n{UBIQUITIN}\n>B\n{UBIQUITIN[:40]}"
        )
        assert err == ""
        assert len(records) == 2
        assert records[0][0] == "A"
        assert records[1][0] == "B"

    def test_parse_seq_lowercase_uppercased(self):
        records, err = cf_mod._parse_fasta_text(
            ">lower\n" + UBIQUITIN.lower()
        )
        assert err == ""
        assert records[0][1] == UBIQUITIN

    def test_parse_empty(self):
        records, err = cf_mod._parse_fasta_text("")
        assert err
        assert records == []
