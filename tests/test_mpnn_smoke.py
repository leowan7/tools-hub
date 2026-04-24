"""Unit tests for the D1 — ProteinMPNN standalone atomic tool.

Covers five things per the ATOMIC-TOOLS.md "Definition of Done":

1. The adapter registers with the right slug, presets, credit costs.
2. ``validate()`` accepts well-formed input and rejects every known
   malformed case (missing fields, out-of-range numerics, empty chains).
3. ``build_payload()`` produces the expected Kendrew job_spec shape.
4. The Flask form template renders and submit validation rejects
   malformed data (feature flag must be flipped ON in the test process
   so the route is not 404'd).
5. The Modal webhook handler accepts a well-formed COMPLETED POST for
   an MPNN job and rejects replay / unknown-job / bad-token cases.

Runs fully offline — no Modal, no Supabase, no Storage. Uses the same
monkey-patching pattern as ``tests/test_jobs_phase4.py`` so CI does not
need GPU access.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from tools import mpnn as mpnn_mod
from tools.base import get as get_adapter


# ---------------------------------------------------------------------------
# Test 1 — adapter registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_adapter_registered_under_mpnn_slug(self):
        adapter = get_adapter("mpnn")
        assert adapter is not None, "tools.mpnn did not register its adapter"
        assert adapter.slug == "mpnn"

    def test_presets_shape(self):
        adapter = get_adapter("mpnn")
        slugs = [p.slug for p in adapter.presets]
        assert slugs == ["smoke", "standalone"]

    def test_credit_costs_match_atomic_spec(self):
        """ATOMIC-TOOLS.md D1: smoke=0 credits, standalone=1 credit."""
        adapter = get_adapter("mpnn")
        smoke = adapter.preset_for("smoke")
        standalone = adapter.preset_for("standalone")
        assert smoke.credits_cost == 0
        assert standalone.credits_cost == 1

    def test_standalone_requires_pdb(self):
        """Per-preset requires_pdb: only standalone needs an upload."""
        adapter = get_adapter("mpnn")
        smoke = adapter.preset_for("smoke")
        standalone = adapter.preset_for("standalone")
        assert smoke.requires_pdb is False
        assert standalone.requires_pdb is True

    def test_templates_point_at_mpnn_partials(self):
        adapter = get_adapter("mpnn")
        assert adapter.form_template == "tools/mpnn_form.html"
        assert adapter.results_partial == "tools/mpnn_results.html"


# ---------------------------------------------------------------------------
# Test 2 — validate() happy path + rejections
# ---------------------------------------------------------------------------


class TestValidate:
    def test_rejects_empty_preset(self):
        inputs, err = mpnn_mod.validate({}, {})
        assert inputs is None
        assert "preset" in (err or "").lower()

    def test_rejects_unknown_preset(self):
        inputs, err = mpnn_mod.validate({"preset": "full"}, {})
        assert inputs is None

    def test_smoke_preset_happy_path(self):
        inputs, err = mpnn_mod.validate({"preset": "smoke"}, {})
        assert err is None
        assert inputs["preset"] == "smoke"
        assert inputs["target_chain"] == "A"
        assert inputs["num_seq_per_target"] == 2
        assert inputs["sampling_temp"] == 0.1

    def test_standalone_happy_path_basic(self):
        form = {
            "preset": "standalone",
            "chains_to_design": "A",
            "num_seq_per_target": "5",
            "sampling_temp": "0.1",
        }
        inputs, err = mpnn_mod.validate(form, {})
        assert err is None, err
        assert inputs["preset"] == "standalone"
        assert inputs["target_chain"] == "A"
        assert inputs["num_seq_per_target"] == 5
        assert inputs["sampling_temp"] == 0.1

    def test_standalone_normalizes_multiple_chains(self):
        """Accept ``A,B``, ``A B``, ``AB`` → all normalize to space-joined."""
        for raw, expected in [
            ("A,B", "A B"),
            ("A B", "A B"),
            ("A, B", "A B"),
            ("A", "A"),
        ]:
            form = {"preset": "standalone", "chains_to_design": raw}
            inputs, err = mpnn_mod.validate(form, {})
            assert err is None, f"{raw!r}: {err}"
            assert inputs["target_chain"] == expected, raw

    def test_standalone_rejects_empty_chains(self):
        form = {"preset": "standalone", "chains_to_design": "   "}
        inputs, err = mpnn_mod.validate(form, {})
        assert inputs is None
        assert err is not None

    def test_standalone_rejects_num_seq_over_cap(self):
        form = {
            "preset": "standalone",
            "chains_to_design": "A",
            "num_seq_per_target": "100",
        }
        inputs, err = mpnn_mod.validate(form, {})
        assert inputs is None
        assert "num_seq_per_target" in (err or "")

    def test_standalone_rejects_num_seq_below_min(self):
        form = {
            "preset": "standalone",
            "chains_to_design": "A",
            "num_seq_per_target": "0",
        }
        inputs, err = mpnn_mod.validate(form, {})
        assert inputs is None

    def test_standalone_rejects_temp_out_of_range(self):
        for bad in ("-0.1", "2.0", "10"):
            form = {
                "preset": "standalone",
                "chains_to_design": "A",
                "sampling_temp": bad,
            }
            inputs, err = mpnn_mod.validate(form, {})
            assert inputs is None, f"temp={bad} should fail"

    def test_standalone_rejects_long_chain_id(self):
        form = {"preset": "standalone", "chains_to_design": "ABCDE"}
        inputs, err = mpnn_mod.validate(form, {})
        assert inputs is None


# ---------------------------------------------------------------------------
# Test 3 — build_payload() shape
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_smoke_payload_shape(self):
        inputs, _ = mpnn_mod.validate({"preset": "smoke"}, {})
        payload = mpnn_mod.build_payload(inputs, presigned_url="")
        assert payload["target_chain"] == "A"
        assert payload["parameters"]["num_seq_per_target"] == 2
        assert payload["parameters"]["sampling_temp"] == 0.1

    def test_standalone_payload_shape(self):
        inputs, _ = mpnn_mod.validate(
            {
                "preset": "standalone",
                "chains_to_design": "A B",
                "num_seq_per_target": "7",
                "sampling_temp": "0.2",
            },
            {},
        )
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        assert payload["target_chain"] == "A B"
        assert payload["parameters"]["num_seq_per_target"] == 7
        assert payload["parameters"]["sampling_temp"] == 0.2
        # Presigned URL is forwarded separately — not embedded.
        assert "presigned" not in json.dumps(payload).lower()


# ---------------------------------------------------------------------------
# Test 4 — Flask form + submit validation
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_mpnn_flag(monkeypatch):
    """Boot the tools-hub Flask app with FLAG_TOOL_MPNN=on so the route
    resolves rather than 404s. Side-effects during create_app (register
    routes) only happen once per process in production, so we build a
    throwaway app against the module-level registry here."""
    monkeypatch.setenv("FLAG_TOOL_MPNN", "on")
    # Session key must be set so login_required's session-bypass in tests works.
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    # Import lazily so the monkeypatched env is in place before create_app runs.
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login_session(client, email="user@example.com"):
    """Set session cookie so ``@login_required`` routes pass."""
    with client.session_transaction() as sess:
        sess["user_email"] = email


def test_form_renders_when_flag_on(app_with_mpnn_flag, monkeypatch):
    """GET /tools/mpnn renders the form when the flag is flipped on."""
    # load_user_context is called on GET; stub it so the page renders
    # without hitting Supabase.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/mpnn")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ProteinMPNN" in body
    assert "Smoke" in body
    assert "Standalone" in body


def test_form_404s_when_flag_off(app_with_mpnn_flag, monkeypatch):
    """With the flag removed, the route must 404 — launch-gate contract."""
    monkeypatch.delenv("FLAG_TOOL_MPNN", raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/mpnn")
    assert resp.status_code == 404


def test_submit_rejects_unknown_preset(app_with_mpnn_flag, monkeypatch):
    """POST with a bad preset rerenders the form with the validation error."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.post(
        "/tools/mpnn/submit",
        data={"preset": "bogus"},
    )
    # Form rerendered with error — not a redirect to job_detail.
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Pick a preset" in body or "preset" in body.lower()


# ---------------------------------------------------------------------------
# Test 5 — Modal webhook handler accepts/rejects correctly
# ---------------------------------------------------------------------------


class TestWebhookRoundtrip:
    """Exercise the shared webhook handler against an MPNN job. We do
    not test the full complete_job pipeline (Supabase dependency); we
    test the handler's response to each auth state and to each payload
    status."""

    def _fake_job(self, status="running", token="t" * 64, tool="mpnn"):
        """Small stand-in that satisfies the attributes the handler uses."""
        from types import SimpleNamespace
        return SimpleNamespace(
            id="job-uuid-1",
            job_token=token,
            status=status,
            tool=tool,
        )

    def test_rejects_unknown_job(self, app_with_mpnn_flag, monkeypatch):
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: None)
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            "/webhooks/modal/missing-job/some-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 404

    def test_rejects_bad_token(self, app_with_mpnn_flag, monkeypatch):
        fake = self._fake_job(token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/wrong-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 403

    def test_accepts_completed_with_good_token(
        self, app_with_mpnn_flag, monkeypatch
    ):
        fake = self._fake_job(status="running", token="good-token")
        # complete_job is CAS-guarded and returns the post-transition
        # row; the handler reads ``.status`` to detect a concurrent
        # cancel. For the happy path we return a fresh row with
        # status=succeeded so the handler takes the "recorded" branch.
        fresh = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        monkeypatch.setattr(
            "webhooks.modal.complete_job",
            lambda *a, **kw: fresh,
        )
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={
                "status": "COMPLETED",
                "output": {
                    "sequences": [{"seq": "MKWVT", "score": 1.1, "recovery": 0.5}],
                    "runtime_seconds": 42,
                },
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "recorded"

    def test_replay_on_terminal_is_noop(
        self, app_with_mpnn_flag, monkeypatch
    ):
        """Replaying the same POST after the job is already terminal
        must not mutate state — returns ``already_terminal``."""
        fake = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "already_terminal"


# ---------------------------------------------------------------------------
# Test 6 — smoke preset shape passed to Modal matches Kendrew payload
# ---------------------------------------------------------------------------


class TestSmokePresetShape:
    def test_app_name_override_maps_mpnn_to_ranomics_namespace(self):
        """Sanity: slug "mpnn" resolves to ``ranomics-mpnn-prod``."""
        from gpu.modal_client import modal_app_name

        assert modal_app_name("mpnn") == "ranomics-mpnn-prod"
        # Non-overridden tools keep the kendrew- prefix.
        assert modal_app_name("bindcraft") == "kendrew-bindcraft-prod"

    def test_preset_gpu_seconds_caps_registered(self):
        """Both MPNN presets have an entry in PRESET_CAPS — the generic
        submit route raises ``ValueError`` otherwise."""
        from gpu.modal_client import preset_gpu_seconds

        assert preset_gpu_seconds("mpnn", "smoke") == 120
        assert preset_gpu_seconds("mpnn", "standalone") == 360

    def test_modal_payload_for_smoke_offline_stub(self, monkeypatch):
        """With modal patched to None, submit returns the deterministic stub.
        Matches the Wave-0 contract that contributors without GPU access still
        get a usable FunctionCall id."""
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = mpnn_mod.validate({"preset": "smoke"}, {})
        payload = mpnn_mod.build_payload(inputs, presigned_url="")
        result = client.submit(
            "mpnn",
            "smoke",
            inputs={**payload, "_input_presigned_url": ""},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="",
        )
        assert result["function_call_id"].startswith("fc-stub-mpnn-smoke-")
        assert result["gpu_seconds_cap"] == 120

    def test_modal_payload_for_standalone_offline_stub(self, monkeypatch):
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = mpnn_mod.validate(
            {
                "preset": "standalone",
                "chains_to_design": "A",
                "num_seq_per_target": "3",
                "sampling_temp": "0.2",
            },
            {},
        )
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        result = client.submit(
            "mpnn",
            "standalone",
            inputs={**payload, "_input_presigned_url": "https://x"},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="https://tools/webhook",
        )
        assert result["function_call_id"].startswith("fc-stub-mpnn-standalone-")
        assert result["gpu_seconds_cap"] == 360


# ---------------------------------------------------------------------------
# Test 7 — run_pipeline.py parser + stub rejection
# ---------------------------------------------------------------------------
#
# The full run_pipeline.main() requires a GPU and the MPNN binary, so we
# can only exercise the parser + stub-rejection logic here. The other
# half of the pipeline (preflight, subprocess call) is covered by the
# live smoke validation the user owes on Modal.


class TestRunPipelineParser:
    def _write_fasta(self, tmp_path, pdb_stem, content):
        """Write MPNN-format FASTA output to ``tmp_path/seqs/<stem>.fa``."""
        seqs_dir = tmp_path / "seqs"
        seqs_dir.mkdir()
        fa = seqs_dir / f"{pdb_stem}.fa"
        fa.write_text(content)
        return tmp_path

    def test_parser_extracts_samples_and_skips_native(self, tmp_path):
        """MPNN emits the native first, then one record per sample. The
        native record has no ``sample=`` metadata and must be skipped."""
        from tools.mpnn import run_pipeline as rp

        content = (
            ">target, score=0.0, fixed_chains=[], designed_chains=['A']\n"
            "AAAAAAAAAA\n"
            ">T=0.1, sample=1, score=1.23, global_score=1.25, "
            "seq_recovery=0.52\n"
            "MKWVAHEDEL\n"
            ">T=0.1, sample=2, score=1.18, global_score=1.20, "
            "seq_recovery=0.48\n"
            "MKWVSHNDQL\n"
        )
        self._write_fasta(tmp_path, "target", content)
        sequences = rp.parse_mpnn_output(tmp_path, pdb_stem="target")
        assert len(sequences) == 2
        assert sequences[0]["seq"] == "MKWVAHEDEL"
        assert sequences[0]["sample"] == 1
        assert sequences[0]["score"] == pytest.approx(1.25)
        assert sequences[0]["recovery"] == pytest.approx(0.52)
        assert sequences[1]["seq"] == "MKWVSHNDQL"

    def test_stub_rejection_on_all_identical_sequences(self):
        """Silent-stub failure mode: every returned sequence is identical.
        reject_stub must ``sys.exit(1)`` via the shared ``_fail`` helper."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "AAAA", "score": 1.0, "recovery": 0.25},
            {"seq": "AAAA", "score": 1.1, "recovery": 0.25},
            {"seq": "AAAA", "score": 1.2, "recovery": 0.25},
        ]
        with pytest.raises(SystemExit):
            rp.reject_stub(sequences)

    def test_stub_rejection_accepts_distinct_sequences(self):
        """Happy path — distinct sequences must not raise."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "MKWVAH", "score": 1.0, "recovery": 0.50},
            {"seq": "MKWVSH", "score": 1.1, "recovery": 0.48},
        ]
        # Must not raise; no return value to assert.
        rp.reject_stub(sequences)

    def test_stub_rejection_on_identical_score_and_recovery(self):
        """Second stub signature: >=3 samples with identical score+recovery."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "MKWVA", "score": 0.96, "recovery": 0.08},
            {"seq": "MKWVS", "score": 0.96, "recovery": 0.08},
            {"seq": "MKWVT", "score": 0.96, "recovery": 0.08},
        ]
        with pytest.raises(SystemExit):
            rp.reject_stub(sequences)
