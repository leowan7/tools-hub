"""Offline unit tests for the OpenDDE co-folding atomic tool.

Mirrors ``tests/test_esmfold_smoke.py``:

1. The adapter registers with the right slug, presets, templates.
2. ``validate()`` accepts well-formed guided + JSON input and rejects every
   known malformed case — including the escape-hatch bound re-application
   (MAX_TOKENS / MAX_CHAINS / sampler ranges) and foreign (AlphaFold3-shaped)
   entity keys.
3. ``build_payload()`` produces the expected job_spec shape with the spec
   delivered inline (no presigned URL).
4. The Flask form renders (flag ON) / 404s (flag OFF); submit rejects bad input.
5. The Modal webhook handler accepts/rejects an OpenDDE job correctly.
6. Pricing is money-safe and in sync (TOOL_SPECS vs PER_JOB_HARD_CAP_USD),
   PRESET_CAPS registered, app name resolves.
7. OpenDDE is kept OUT of every campaign set (it is a plain atomic tool).
8. The offline Modal submit stub round-trips.
9. ``_sanitize_candidate`` keeps the new ``ranking_score`` field.
10. QC-hardening regressions from the adversarial review.
11. ``run_pipeline.archive_raw_outputs`` — the destination resolves on call, and
    the function honours its documented "never raises" contract.

Runs fully offline — no Modal, no Supabase, no GPU.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import tarfile
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tools import opendde as odde
from tools.base import get as get_adapter
from tools.opendde import run_pipeline as rp


UBIQUITIN = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"


# ---------------------------------------------------------------------------
# 1 — adapter registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_registered(self):
        adapter = get_adapter("opendde")
        assert adapter is not None
        assert adapter.slug == "opendde"

    def test_presets(self):
        adapter = get_adapter("opendde")
        assert [p.slug for p in adapter.presets] == ["general", "abag"]

    def test_no_pdb(self):
        adapter = get_adapter("opendde")
        assert adapter.requires_pdb is False
        for p in adapter.presets:
            assert p.requires_pdb is False

    def test_templates(self):
        adapter = get_adapter("opendde")
        assert adapter.form_template == "tools/opendde_form.html"
        assert adapter.results_partial == "tools/opendde_results.html"


# ---------------------------------------------------------------------------
# 2 — validate(): guided + json happy paths and rejections
# ---------------------------------------------------------------------------


def _guided(**over):
    form = {"preset": "general", "spec_mode": "guided", "proteins": f">A\n{UBIQUITIN}"}
    form.update(over)
    return form


class TestValidateGuided:
    def test_happy_protein(self):
        inputs, err = odde.validate(_guided(), {})
        assert err is None, err
        assert inputs["preset"] == "general"
        spec = inputs["spec"]
        assert isinstance(spec, list) and len(spec) == 1
        seqs = spec[0]["sequences"]
        assert seqs[0]["proteinChain"]["sequence"] == UBIQUITIN
        assert inputs["parameters"]["n_designs_total"] == 1

    def test_protein_ligand(self):
        inputs, err = odde.validate(_guided(ligands="CCD_ATP"), {})
        assert err is None, err
        seqs = inputs["spec"][0]["sequences"]
        kinds = [list(e)[0] for e in seqs]
        assert "proteinChain" in kinds
        assert kinds.count("ligand") == 1

    def test_smiles_ligand_kept_bare(self):
        inputs, err = odde.validate(_guided(ligands="CC(=O)Oc1ccccc1C(=O)O"), {})
        assert err is None, err
        lig = [e for e in inputs["spec"][0]["sequences"] if "ligand" in e][0]
        # OpenDDE wraps the ligand in an object; the SMILES code itself is kept
        # bare (no SMILES: prefix injected) per the verified schema.
        assert lig["ligand"]["ligand"] == "CC(=O)Oc1ccccc1C(=O)O"

    def test_guided_ligand_object_shape(self):
        # OpenDDE's json_parser.build_ligand indexes info["ligand"], so a bare
        # string {"ligand": "CCD_ATP"} crashes it (verified at the O-2 canary).
        # The adapter must emit the object form with count + id.
        inputs, err = odde.validate(_guided(ligands="CCD_ATP"), {})
        assert err is None, err
        seqs = inputs["spec"][0]["sequences"]
        lig = [e for e in seqs if "ligand" in e][0]["ligand"]
        assert lig["ligand"] == "CCD_ATP" and lig["count"] == 1 and len(lig["id"]) == 1
        # ids are globally unique across all entities (polymer + ligand).
        all_ids = [i for e in seqs for v in e.values() for i in v.get("id", [])]
        assert len(all_ids) == len(set(all_ids))

    def test_guided_ion_rejected(self):
        # OpenDDE v1 preview cannot featurize ions; blocked PRE-GPU so no user is
        # charged for a guaranteed failure (verified at the O-2 / iso canaries).
        inputs, err = odde.validate(_guided(ligands="CCD_ATP", ions="MG"), {})
        assert inputs is None
        assert "ion" in (err or "").lower() and "not supported" in (err or "").lower()

    def test_dna_rna(self):
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "guided",
             "dna": ">D\nATGCATGC", "rna": ">R\nAUGCAUGC"}, {}
        )
        assert err is None, err
        kinds = [list(e)[0] for e in inputs["spec"][0]["sequences"]]
        assert "dnaSequence" in kinds and "rnaSequence" in kinds

    def test_reject_bad_preset(self):
        inputs, err = odde.validate(_guided(preset="bogus"), {})
        assert inputs is None and err

    def test_reject_bad_spec_mode(self):
        inputs, err = odde.validate(_guided(spec_mode="xml"), {})
        assert inputs is None and err

    def test_reject_non_canonical_protein(self):
        inputs, err = odde.validate(_guided(proteins=">A\nMQIFBJOZ"), {})
        assert inputs is None
        assert "invalid" in (err or "").lower()

    def test_reject_bad_dna(self):
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "guided", "dna": ">D\nATGCU"}, {}
        )
        assert inputs is None  # U is not a DNA base

    def test_reject_empty_guided(self):
        inputs, err = odde.validate({"preset": "general", "spec_mode": "guided"}, {})
        assert inputs is None
        assert "at least one" in (err or "").lower()

    def test_reject_bad_ion(self):
        inputs, err = odde.validate(_guided(ions="MAGNESIUM7"), {})
        assert inputs is None

    def test_reject_too_many_tokens(self):
        # Two 1200-aa chains = 2400 tokens > MAX_TOKENS (each chain < MAX_SEQ_LEN).
        big = "A" * 1200
        inputs, err = odde.validate(
            _guided(proteins=f">A\n{big}\n>B\n{big}"), {}
        )
        assert inputs is None
        assert "too large" in (err or "").lower()

    def test_reject_oversize_single_chain(self):
        inputs, err = odde.validate(_guided(proteins=">A\n" + "A" * 2000), {})
        assert inputs is None
        assert "too long" in (err or "").lower()

    @pytest.mark.parametrize(
        "field,value",
        [("sample", "9"), ("step", "9999"), ("cycle", "99"), ("n_seeds", "50")],
    )
    def test_reject_sampler_out_of_range(self, field, value):
        inputs, err = odde.validate(_guided(**{field: value}), {})
        assert inputs is None and err

    def test_n_designs_total_is_seeds_times_samples(self):
        inputs, err = odde.validate(_guided(sample="3", n_seeds="2"), {})
        assert err is None, err
        assert inputs["parameters"]["n_designs_total"] == 6
        assert inputs["spec"][0]["modelSeeds"] == [1, 2]


class TestValidateJson:
    def _spec(self, seqs, name="job", seeds=None):
        job = {"name": name, "sequences": seqs}
        if seeds is not None:
            job["modelSeeds"] = seeds
        return json.dumps([job])

    def test_happy(self):
        spec = self._spec([{"proteinChain": {"sequence": UBIQUITIN, "count": 1, "id": ["A"]}}])
        inputs, err = odde.validate(
            {"preset": "abag", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert err is None, err
        assert inputs["preset"] == "abag"
        assert inputs["spec"][0]["sequences"][0]["proteinChain"]["sequence"] == UBIQUITIN

    def test_reject_malformed_json(self):
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": "{not json"}, {}
        )
        assert inputs is None
        assert "json" in (err or "").lower()

    def test_reject_foreign_entity_key(self):
        # AlphaFold3-style shorthand "protein" is not an OpenDDE entity key.
        spec = self._spec([{"protein": {"sequence": UBIQUITIN, "count": 1, "id": ["A"]}}])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert inputs is None
        assert "unknown type" in (err or "").lower()

    def test_reject_id_count_mismatch(self):
        spec = self._spec([{"proteinChain": {"sequence": UBIQUITIN, "count": 2, "id": ["A"]}}])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert inputs is None  # id list length must equal count

    def test_escape_hatch_reapplies_token_cap(self):
        big = "A" * 1200
        spec = self._spec([
            {"proteinChain": {"sequence": big, "count": 1, "id": ["A"]}},
            {"proteinChain": {"sequence": big, "count": 1, "id": ["B"]}},
        ])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert inputs is None
        assert "too large" in (err or "").lower()

    def test_escape_hatch_reapplies_chain_cap(self):
        seqs = [{"ligand": {"ligand": "CCD_ATP", "count": 1}} for _ in range(odde.MAX_CHAINS + 1)]
        spec = self._spec(seqs)
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert inputs is None
        assert "chains" in (err or "").lower()

    def test_reject_multi_job_list(self):
        two = json.dumps([
            {"name": "a", "sequences": [{"proteinChain": {"sequence": UBIQUITIN, "count": 1}}]},
            {"name": "b", "sequences": [{"proteinChain": {"sequence": UBIQUITIN, "count": 1}}]},
        ])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": two}, {}
        )
        assert inputs is None
        assert "one job" in (err or "").lower()

    def test_modelseeds_overridden_by_form(self):
        spec = self._spec(
            [{"proteinChain": {"sequence": UBIQUITIN, "count": 1, "id": ["A"]}}],
            seeds=[999],
        )
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec,
             "seed": "5", "n_seeds": "2"}, {}
        )
        assert err is None, err
        # The pasted modelSeeds [999] is replaced by the form-driven seeds.
        assert inputs["spec"][0]["modelSeeds"] == [5, 6]


# ---------------------------------------------------------------------------
# 3 — build_payload()
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_shape(self):
        inputs, _ = odde.validate(_guided(), {})
        payload = odde.build_payload(inputs, presigned_url="https://ignored")
        assert set(payload) == {
            "preset", "spec", "sample", "step", "cycle", "n_designs_total", "parameters",
        }
        assert isinstance(payload["spec"], list)
        # Spec travels inline — no presigned URL embedded.
        assert "https://ignored" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# 4 — Flask form + submit
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_flag(monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_OPENDDE", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login(client, email="user@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def _patch_ctx(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "blueprints.tools.load_user_context",
        lambda: SimpleNamespace(user_id="u1", tier="free", balance=10, email="user@example.com"),
    )


def test_form_renders_when_flag_on(app_with_flag, monkeypatch):
    _patch_ctx(monkeypatch)
    client = app_with_flag.test_client()
    _login(client)
    resp = client.get("/tools/opendde")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "OpenDDE" in body
    assert 'name="preset" value="general"' in body
    assert 'name="preset" value="abag"' in body
    assert 'name="spec_mode" value="guided"' in body
    assert 'name="spec_mode" value="json"' in body
    assert 'name="proteins"' in body
    assert 'name="spec_json"' in body


def test_form_404s_when_flag_off(app_with_flag, monkeypatch):
    monkeypatch.delenv("FLAG_TOOL_OPENDDE", raising=False)
    _patch_ctx(monkeypatch)
    client = app_with_flag.test_client()
    _login(client)
    # Adapter is still registered — the 404 is the flag gate, not a missing tool.
    assert get_adapter("opendde") is not None
    resp = client.get("/tools/opendde")
    assert resp.status_code == 404


def test_submit_rejects_bad_preset(app_with_flag, monkeypatch):
    _patch_ctx(monkeypatch)
    client = app_with_flag.test_client()
    _login(client)
    resp = client.post("/tools/opendde/submit", data={"preset": "bogus", "spec_mode": "guided"})
    assert resp.status_code == 200
    assert "preset" in resp.get_data(as_text=True).lower()


# ---------------------------------------------------------------------------
# 5 — Modal webhook roundtrip
# ---------------------------------------------------------------------------


class TestWebhookRoundtrip:
    def _fake_job(self, status="running", token="t" * 64):
        from types import SimpleNamespace

        return SimpleNamespace(
            id="job-uuid-1", job_token=token, status=status, tool="opendde", user_id="user-uuid-1",
        )

    def test_unknown_job_404(self, app_with_flag, monkeypatch):
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: None)
        client = app_with_flag.test_client()
        resp = client.post("/webhooks/modal/missing/tok", json={"status": "COMPLETED", "output": {}})
        assert resp.status_code == 404

    def test_bad_token_403(self, app_with_flag, monkeypatch):
        fake = self._fake_job(token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_flag.test_client()
        resp = client.post(f"/webhooks/modal/{fake.id}/wrong", json={"status": "COMPLETED", "output": {}})
        assert resp.status_code == 403

    def test_completed_200(self, app_with_flag, monkeypatch):
        fake = self._fake_job(status="running", token="good-token")
        fresh = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        monkeypatch.setattr("webhooks.modal.complete_job", lambda *a, **kw: fresh)
        client = app_with_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={"status": "COMPLETED", "output": {"status": "COMPLETED", "designs": []}},
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "recorded"

    def test_replay_already_terminal(self, app_with_flag, monkeypatch):
        fake = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_flag.test_client()
        resp = client.post(f"/webhooks/modal/{fake.id}/good-token", json={"status": "COMPLETED", "output": {}})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "already_terminal"


# ---------------------------------------------------------------------------
# 6 — pricing wiring + money safety
# ---------------------------------------------------------------------------


class TestPricingWiring:
    def test_tool_spec(self):
        from shared import wallet_estimates as we

        spec = we.TOOL_SPECS["opendde"]
        assert spec.gpu_class == "H100"
        # Fixed single-container budget — no per-design fan-out that could under-hold.
        assert spec.scaling_param is None
        assert spec.base_hard_cap_usd == Decimal("15.00")

    def test_hard_cap_covers_worst_case_container(self):
        """Hold ceiling must be >= a full-session H100 container's charge so a
        single heavy job can never under-hold."""
        from shared import wallet_estimates as we

        worst = (
            Decimal("3600")
            * Decimal(str(we.GPU_USD_PER_SECOND["H100"]))
            * we.WALLET_MARKUP
        )
        cap = we.compute_hard_cap("opendde", {"n_designs_total": 16})
        assert cap == Decimal("15.00")
        assert cap >= worst  # $15.00 >= ~$14.79

    def test_caps_in_sync(self):
        from shared import wallet as w
        from shared import wallet_estimates as we

        assert w.PER_JOB_HARD_CAP_USD["opendde"] == we.TOOL_SPECS["opendde"].absolute_cap_usd
        assert w.PER_JOB_HARD_CAP_USD["opendde"] == Decimal("15.00")

    def test_preset_caps(self):
        from gpu import modal_client as mc

        assert mc.preset_gpu_seconds("opendde", "general") == 3600
        assert mc.preset_gpu_seconds("opendde", "abag") == 3600
        assert mc.modal_app_name("opendde") == "ranomics-opendde-prod"


# ---------------------------------------------------------------------------
# 7 — atomic, NOT a campaign tool
# ---------------------------------------------------------------------------


class TestNotACampaignTool:
    def test_absent_from_campaign_sets(self):
        from shared import compute_campaigns as cc

        assert "opendde" not in cc.SUPPORTED_TOOLS
        assert "opendde" not in cc.CAMPAIGN_ONLY_TOOLS
        assert "opendde" not in cc._FIXED_CONTAINER_TOOLS


# ---------------------------------------------------------------------------
# 8 — offline Modal submit stub
# ---------------------------------------------------------------------------


def test_modal_submit_offline_stub(monkeypatch):
    from gpu import modal_client as mc

    monkeypatch.setattr(mc, "_import_modal", lambda: None)
    client = mc.ModalClient(environment="main")
    inputs, _ = odde.validate(_guided(), {})
    payload = odde.build_payload(inputs, presigned_url="")
    result = client.submit(
        "opendde", "general",
        inputs={**payload, "_input_presigned_url": ""},
        job_id="job-xyz", job_token="tok", webhook_url="https://tools/webhook",
    )
    assert result["function_call_id"].startswith("fc-stub-opendde-general-")
    assert result["gpu_seconds_cap"] == 3600


# ---------------------------------------------------------------------------
# 9 — webhook candidate sanitiser keeps ranking_score
# ---------------------------------------------------------------------------


def test_sanitize_candidate_keeps_ranking_score():
    from webhooks.modal import _sanitize_candidate

    out = _sanitize_candidate(
        {"rank": 0, "name": "prediction_0", "pdb_key": "prediction_0.pdb", "ranking_score": 0.873}
    )
    assert out is not None
    assert out["ranking_score"] == 0.873
    assert out["pdb_key"] == "prediction_0.pdb"


# ---------------------------------------------------------------------------
# 10 — QC-hardening regressions (fixes from the adversarial review)
# ---------------------------------------------------------------------------


class TestQCFixes:
    def test_hold_floor_survives_low_p90(self, monkeypatch):
        """Once historical p90 pulls the estimate down, the HOLD must still cover
        the fixed-container worst case (3600 s H100 ~= $14.79) — the p90 branch
        must not shrink the hold below the physical session budget."""
        from shared import wallet_estimates as we

        monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: 300.0)
        # The displayed estimate really did drop with the low p90 ...
        est = we.estimated_cost_for_tool(None, "opendde", {})
        assert est < Decimal("2.00")
        # ... but the hold is floored at the worst-case container charge.
        hold = we.cushioned_hold_usd(None, "opendde", {})
        assert hold >= Decimal("14.79")
        assert hold <= Decimal("15.00")

    def test_hold_bootstrap_is_capped(self, monkeypatch):
        from shared import wallet_estimates as we

        monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)
        assert we.cushioned_hold_usd(None, "opendde", {}) == Decimal("15.00")

    def test_json_ion_object_rejected(self):
        # Even a perfectly well-formed ion object is blocked (OpenDDE v1 preview
        # cannot featurize ions) — rejected PRE-GPU with the specific message.
        spec = json.dumps([{"name": "j", "sequences": [{"ion": {"ion": "MG", "count": 1}}]}])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert inputs is None and "not supported" in (err or "").lower()

    def test_json_deep_nesting_returns_clean_error(self):
        # Must return (None, error), never raise RecursionError -> 500.
        payload = ("[" * 20000) + ("]" * 20000)
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": payload}, {}
        )
        assert inputs is None and err

    def test_json_oversize_rejected(self):
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": "x" * 200_001}, {}
        )
        assert inputs is None
        assert "too large" in (err or "").lower()

    def test_json_ligand_length_bounded(self):
        spec = json.dumps([{"name": "j", "sequences": [{"ligand": {"ligand": "C" * 600, "count": 1}}]}])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert inputs is None
        assert "too long" in (err or "").lower()

    def test_json_rejects_bare_string_ligand(self):
        # The exact O-2 bug: a bare-string ligand must be rejected up front (object
        # form required), never passed through to crash OpenDDE's parser.
        spec = json.dumps([{"name": "j", "sequences": [{"ligand": "CCD_ATP"}]}])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert inputs is None and "object" in (err or "").lower()

    def test_json_accepts_object_ligand(self):
        spec = json.dumps([{"name": "j", "sequences": [
            {"proteinChain": {"sequence": UBIQUITIN, "count": 1}},
            {"ligand": {"ligand": "CCD_ATP", "count": 1}},
        ]}])
        inputs, err = odde.validate(
            {"preset": "general", "spec_mode": "json", "spec_json": spec}, {}
        )
        assert err is None, err
        seqs = inputs["spec"][0]["sequences"]
        assert [list(e)[0] for e in seqs] == ["proteinChain", "ligand"]
        assert seqs[1]["ligand"]["ligand"] == "CCD_ATP"

    def test_ranking_score_has_glossary_entry(self):
        from shared.metric_glossary import GLOSSARY

        assert "ranking_score" in GLOSSARY
        assert GLOSSARY["ranking_score"].get("label")


# ---------------------------------------------------------------------------
# 11 — archive_raw_outputs (run_pipeline)
# ---------------------------------------------------------------------------


def _work_tree(tmp_path):
    src = tmp_path / "opendde_work"
    src.mkdir()
    (src / "prediction_0.pdb").write_text("ATOM      1  N   MET A   1\nEND\n")
    return src


class TestRawArchiveDest:
    """``dest`` must be resolved when the function is called.

    ``def archive_raw_outputs(work_dir, dest=RAW_ARCHIVE_PATH)`` evaluates the
    constant once, at def time, and binds its VALUE into the function object.
    Reassigning the module constant afterwards is then silently ignored and the
    tar lands on the real ``/tmp`` path regardless.

    Not hypothetical: the proteina copy of this function carried exactly that
    default, and the test harness that set ``RAW_ARCHIVE_PATH`` to keep archives
    inside ``tmp_path`` had been writing to the real path the whole time. No
    opendde test set the constant before now, so here it was latent rather than
    live — the trap was waiting for the first test that tried.
    """

    def test_default_follows_a_reassigned_constant(self, tmp_path, monkeypatch):
        src = _work_tree(tmp_path)
        redirected = tmp_path / "redirected.tgz"
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(redirected))

        rp.archive_raw_outputs(str(src))

        assert redirected.is_file(), (
            "archive_raw_outputs ignored the reassigned RAW_ARCHIVE_PATH — its "
            "default is frozen at import, so the tar went to the real /tmp path")
        with tarfile.open(redirected) as tf:
            assert any(n.endswith("prediction_0.pdb") for n in tf.getnames())

    def test_explicit_dest_still_wins(self, tmp_path, monkeypatch):
        src = _work_tree(tmp_path)
        constant = tmp_path / "constant.tgz"
        explicit = tmp_path / "explicit.tgz"
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(constant))

        rp.archive_raw_outputs(str(src), dest=str(explicit))

        assert explicit.is_file()
        assert not constant.exists(), "an explicit dest was overridden by the constant"

    def test_an_empty_dest_is_honoured_as_given(self, tmp_path, monkeypatch, caplog):
        """ONLY None resolves — the guard tests identity, not truthiness.

        ``if not dest`` satisfies every other test in this class, and it would
        substitute the module constant for a caller's explicit falsy dest, so
        the tar lands somewhere the caller never asked for. The empty string is
        the reachable falsy case: ``os.path.abspath("")`` is the cwd, which is a
        directory, so the write fails there — and the failure has to happen
        THERE rather than being diverted to RAW_ARCHIVE_PATH. Nothing is cleaned
        up on this path: the handler's os.remove() on the cwd raises and is
        swallowed, which is right — a directory must not be unlinked. chdir into
        tmp_path to keep the doomed write local.

        The last two assertions are what stop this degrading to a vacuous test.
        The ``is None`` and ``not constant.exists()`` checks are both negative,
        and inaction satisfies them: put an early ``return`` at the top of
        archive_raw_outputs and the two behavioural tests in this class go red
        while this one stays green (the signature test stays green too — it only
        inspects the signature). Recording the path handed to tarfile.open pins that the
        cwd-directed write was actually ATTEMPTED, and the warning pins that it
        then failed there rather than being quietly skipped.
        """
        src = _work_tree(tmp_path)
        constant = tmp_path / "constant.tgz"
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(constant))
        monkeypatch.chdir(tmp_path)
        # Exactly what the function computes from dest="". Read after the chdir,
        # and via abspath rather than str(tmp_path) so a symlinked tmp dir agrees.
        cwd_target = os.path.abspath("")

        attempted = []
        real_tar_open = rp.tarfile.open

        def recording_open(name, *args, **kwargs):
            # Record the path the resolution produced, then let the real open
            # fail on it exactly as it would unpatched (the errno is
            # platform-dependent, so do not assert on the exception itself).
            attempted.append(name)
            return real_tar_open(name, *args, **kwargs)

        monkeypatch.setattr(rp, "tarfile", SimpleNamespace(open=recording_open))

        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            assert rp.archive_raw_outputs(str(src), dest="") is None

        assert not constant.exists(), (
            'dest="" was replaced by RAW_ARCHIVE_PATH — the resolution is '
            "testing truthiness instead of identity, so an explicit falsy dest "
            "is silently overridden")
        assert attempted == [cwd_target], (
            f'dest="" never reached the write: expected one tar write aimed at the '
            f"cwd ({cwd_target}), got {attempted!r}")
        assert sum("raw capture failed" in r.getMessage() for r in caplog.records) == 1, (
            "the doomed cwd write was not reported as a capture failure; records "
            f"were {[r.getMessage() for r in caplog.records]!r}")

    def test_signature_default_is_not_a_baked_in_path(self):
        """Once nothing reassigns the constant the regression is behaviourally
        invisible, so pin the signature as well as the behaviour."""
        default = inspect.signature(rp.archive_raw_outputs).parameters["dest"].default
        assert default is None, (
            f"dest defaults to {default!r}; a module-constant default is bound at "
            "import and cannot follow a later reassignment of RAW_ARCHIVE_PATH")


class TestRawArchiveNeverRaises:
    """The cleanup handler must not raise on top of the failure it is cleaning up.

    Contract hardening, not a fix for anything observed in production: the sole
    call site passes an absolute str, so the window below is not reachable
    there. It is pinned because the function is documented "never raises" and is
    called from a ``finally`` in ``main()``, where an escape replaces whatever
    exit was already in flight. The handler deletes a partial tar at
    ``dest_abs``, but ``dest_abs`` used to be assigned partway through the
    ``try``: any failure before that line left it unbound, and the resulting
    UnboundLocalError is a NameError, which the inner ``except OSError`` does
    not catch.
    """

    def test_failure_before_dest_is_bound_does_not_escape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(tmp_path / "raw.tgz"))

        class _NotAPath:
            # os.path.isdir() swallows OSError and ValueError but NOT TypeError,
            # so this reaches the except block with dest_abs still unassigned —
            # the first statement in the try that can throw past the guard.
            def __fspath__(self):
                raise TypeError("simulated: work_dir is not a usable path")

        # Must return normally. Pre-fix this raised UnboundLocalError.
        assert rp.archive_raw_outputs(_NotAPath()) is None

    def test_partial_tar_is_still_removed_when_dest_is_bound(self, tmp_path, monkeypatch):
        """The None-guard must not disable the cleanup it guards.

        When the write fails AFTER dest_abs is bound, the truncated tar still has
        to go: modal_app parks whatever file exists, and a tar that reports
        success but cannot be read is worse than no tar at all.
        """
        src = _work_tree(tmp_path)
        dest = tmp_path / "raw.tgz"

        def exploding_open(name, mode="r", *args, **kwargs):
            # Leave the truncated file a real mid-write ENOSPC would leave behind.
            with open(name, "wb") as fh:
                fh.write(b"not a readable tar")
            raise OSError(28, "No space left on device")

        # Patch the name inside the module's namespace, not the shared stdlib
        # module object, so nothing outside this call is affected.
        monkeypatch.setattr(rp, "tarfile", SimpleNamespace(open=exploding_open))

        rp.archive_raw_outputs(str(src), dest=str(dest))

        assert not dest.exists(), (
            "the partial tar survived a failed capture; the wrapper would park an "
            "archive that reports success and cannot be read")
