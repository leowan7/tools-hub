"""Offline unit tests for the Proteina-Complexa campaign tool.

Runs fully offline — no Modal, no Supabase, no GPU. Covers:

1. Adapter registration (slug, 4 presets, campaign-only, templates).
2. ``validate()`` per preset — config_name mapping, default task names (incl.
   the M-prefixed AME task, NOT the non-existent "01_AME"), rf3_required flags,
   and the reject cases.
3. ``build_payload()`` shape.
4. Pricing + campaign wiring (TOOL_SPECS / PER_JOB_HARD_CAP_USD / PRESET_CAPS /
   chunk size / fixed-container / campaign-only / launch concurrency /
   first-wave hold).
5. ``run_pipeline`` pure helpers — deterministic distinct seeds, the RF3
   kill-switch env parse, the design CLI overrides, and the tolerant reward-CSV
   parser + PDB matching against a synthetic run dir (the output-layer guard).
6. The results template parses.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tools import proteina as px
from tools.base import get as get_adapter


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_registered(self):
        a = get_adapter("proteina")
        assert a is not None and a.slug == "proteina"

    def test_presets(self):
        a = get_adapter("proteina")
        assert [p.slug for p in a.presets] == [
            "protein_binder", "ligand_binder", "motif_ame", "validate",
        ]

    def test_requires_pdb_false(self):
        a = get_adapter("proteina")
        assert a.requires_pdb is False

    def test_templates(self):
        a = get_adapter("proteina")
        assert a.results_partial == "tools/proteina_results.html"

    def test_campaign_only(self):
        from shared import compute_campaigns as cc
        assert "proteina" in cc.CAMPAIGN_ONLY_TOOLS


# ---------------------------------------------------------------------------
# 2. validate()
# ---------------------------------------------------------------------------


class TestValidateAccept:
    def test_protein_binder_defaults(self):
        inp, err = px.validate({"preset": "protein_binder"}, {})
        assert err is None
        assert inp["config_name"] == "search_binder_local_pipeline"
        assert inp["task_name"] == "02_PDL1"
        assert inp["rf3_required"] is False
        assert inp["target_chain"] == "A"
        assert inp["designs_per_shard"] == 8

    def test_ligand_binder_defaults(self):
        inp, err = px.validate({"preset": "ligand_binder"}, {})
        assert err is None
        assert inp["config_name"] == "search_ligand_binder_local_pipeline"
        assert inp["task_name"] == "39_7V11_LIGAND"
        assert inp["rf3_required"] is True
        # ligand target is an SDF -> no protein chain
        assert inp["target_chain"] == ""

    def test_motif_ame_default_task_is_real(self):
        # Regression: the default AME task must be a real M-prefixed name, not
        # the non-existent "01_AME" (which would be a billed GPU failure).
        inp, err = px.validate({"preset": "motif_ame"}, {})
        assert err is None
        assert inp["config_name"] == "search_ame_local_pipeline"
        assert inp["task_name"] == "M0024_1nzy"
        assert not inp["task_name"].endswith("AME")
        assert inp["rf3_required"] is True

    def test_validate_preset(self):
        inp, err = px.validate({"preset": "validate"}, {})
        assert err is None
        assert inp["config_name"] is None
        assert inp["rf3_required"] is False

    def test_custom_task_name(self):
        inp, err = px.validate(
            {"preset": "protein_binder", "task_name": "38_TNFalpha"}, {})
        assert err is None and inp["task_name"] == "38_TNFalpha"

    def test_protein_binder_custom_chain(self):
        inp, err = px.validate(
            {"preset": "protein_binder", "task_name": "02_PDL1", "target_chain": "B"}, {})
        assert err is None and inp["target_chain"] == "B"


class TestValidateReject:
    def test_bad_preset(self):
        _, err = px.validate({"preset": "nope"}, {})
        assert err is not None

    def test_bad_task_name(self):
        _, err = px.validate(
            {"preset": "protein_binder", "task_name": "bad name!$"}, {})
        assert err and "task name" in err.lower()

    def test_chain_too_long(self):
        _, err = px.validate(
            {"preset": "protein_binder", "task_name": "02_PDL1", "target_chain": "ABCDE"}, {})
        assert err and "chain" in err.lower()


# ---------------------------------------------------------------------------
# 3. build_payload
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_shape(self):
        inp, err = px.validate({"preset": "ligand_binder"}, {})
        assert err is None
        bp = px.build_payload(inp, "https://example/presigned")
        assert set(bp) == {
            "preset", "config_name", "task_name", "target_chain",
            "rf3_required", "nsamples", "replicas", "nsteps", "parameters",
        }
        assert bp["parameters"]["n_designs_total"] == 8


# ---------------------------------------------------------------------------
# 4. Pricing + campaign wiring
# ---------------------------------------------------------------------------


class TestPricingWiring:
    def test_tool_specs(self):
        from shared import wallet_estimates as we
        spec = we.TOOL_SPECS["proteina"]
        assert spec.gpu_class == "A100-80GB"
        assert spec.scaling_param == "num_designs"

    def test_hard_cap_mirror(self):
        from shared import wallet as w
        from shared import wallet_estimates as we
        assert w.PER_JOB_HARD_CAP_USD["proteina"] == Decimal("60.00")
        assert we.TOOL_SPECS["proteina"].absolute_cap_usd == Decimal("60.00")

    def test_preset_caps(self):
        from gpu import modal_client as mc
        for p in ("protein_binder", "ligand_binder", "motif_ame"):
            assert mc.preset_gpu_seconds("proteina", p) == 7200
        assert mc.preset_gpu_seconds("proteina", "validate") == 900
        assert mc.modal_app_name("proteina") == "ranomics-proteina-prod"

    def test_campaign_registries(self):
        from shared import compute_campaigns as cc
        assert "proteina" in cc.SUPPORTED_TOOLS
        assert cc._CHUNK_SIZE_OVERRIDE["proteina"] == 8
        assert "proteina" in cc._FIXED_CONTAINER_TOOLS
        assert cc.launch_concurrency_for("proteina") == 4
        # a live tool keeps the default concurrency
        assert cc.launch_concurrency_for("boltzgen") == cc.DEFAULT_CONCURRENCY_TARGET

    def test_first_wave_hold_bounded(self):
        # Fixed-container tool: first-wave hold prices launch_concurrency (4)
        # containers, NOT per-design. Must be well under a runaway number.
        from shared import compute_campaigns as cc
        plan = cc.plan_chunks("proteina", 100, "protein_binder")
        hold = cc.first_wave_hold_usd(plan, cc.launch_concurrency_for("proteina"))
        assert Decimal("0") < hold <= Decimal("70.00")


# ---------------------------------------------------------------------------
# 5. run_pipeline pure helpers
# ---------------------------------------------------------------------------


class TestSeed:
    def test_deterministic_and_distinct(self):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        a = rp.shard_seed("job-abc")
        b = rp.shard_seed("job-abc")
        c = rp.shard_seed("job-xyz")
        assert a == b and a != c
        assert 0 <= a < 1_000_000

    def test_empty_job_id(self):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        assert rp.shard_seed("") == 42


class TestRf3Switch:
    def test_default_on(self, monkeypatch):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        monkeypatch.delenv("PROTEINA_RF3", raising=False)
        assert rp._rf3_enabled() is True

    @pytest.mark.parametrize("val", ["off", "false", "0", "no", "OFF"])
    def test_off_values(self, monkeypatch, val):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        monkeypatch.setenv("PROTEINA_RF3", val)
        assert rp._rf3_enabled() is False


class TestDesignCmd:
    def test_overrides_present(self):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        cmd = rp.build_design_cmd(
            config_name="search_binder_local_pipeline", task_name="02_PDL1",
            seed=123, nsamples=4, replicas=2, nsteps=400, run_name="shard_x",
            rf3_on=True)
        joined = " ".join(cmd)
        assert "search_binder_local_pipeline.yaml" in joined
        assert "++seed=123" in cmd
        assert "++job_id=0" in cmd
        assert "++generation.task_name=02_PDL1" in cmd
        assert "++generation.filter.delete_non_top_n_samples=false" in cmd
        assert any("filter_samples_limit" in c for c in cmd)
        # designs/shard pinned explicitly (4 x 2 = 8) so every variant matches
        # the campaign chunk_size regardless of its config default.
        assert "++generation.dataloader.dataset.nres.nsamples=4" in cmd
        assert "++generation.search.best_of_n.replicas=2" in cmd
        # RF3 on -> no disable override
        assert not any("use_rf3=false" in c for c in cmd)

    def test_rf3_off_override(self):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        cmd = rp.build_design_cmd(
            config_name="search_binder_local_pipeline", task_name="02_PDL1",
            seed=1, nsamples=4, replicas=2, nsteps=None, run_name="s", rf3_on=False)
        assert any("use_rf3=false" in c for c in cmd)


class TestRewardParse:
    def _make_run(self, tmp_path):
        # A synthetic reward CSV + matching PDBs under a nested run dir.
        run = tmp_path / "run"
        sub = run / "inference" / "samples"
        sub.mkdir(parents=True)
        (sub / "design_A.pdb").write_text("ATOM\n")
        (sub / "design_B.pdb").write_text("ATOM\n")
        csv_text = (
            "sample,total_reward,af2_iptm,plddt,rf3,scrmsd,cluster\n"
            "design_A,0.40,0.70,88.0,0.55,1.2,0\n"
            "design_B,0.90,0.85,92.0,0.61,0.8,1\n"
        )
        (run / "inference" / "rewards_search_binder_local_pipeline_0.csv").write_text(csv_text)
        return run

    def test_parse_and_rank(self, tmp_path):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        run = self._make_run(tmp_path)
        designs = rp.parse_designs(run)
        assert len(designs) == 2
        # ranked by total_reward desc -> design_B first
        assert designs[0]["name"] == "design_B"
        assert designs[0]["rank"] == 0
        s = designs[0]["scores"]
        assert s["total_reward"] == 0.9
        assert s["af2_iptm"] == 0.85
        assert s["af2_plddt"] == 92.0
        assert s["rf3_score"] == 0.61
        assert s["binder_scrmsd"] == 0.8
        assert s["cluster_id"] == 1 and isinstance(s["cluster_id"], int)

    def test_pdb_match(self, tmp_path):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        run = self._make_run(tmp_path)
        designs = rp.parse_designs(run)
        top = designs[0]
        pdb = rp.find_pdb_for(top, run, top["_row_index"], len(designs))
        assert pdb is not None and pdb.name == "design_B.pdb"

    def test_no_csv_returns_empty(self, tmp_path):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        empty = tmp_path / "empty"
        empty.mkdir()
        assert rp.parse_designs(empty) == []


class TestPreGpuGuards:
    """main() must FAIL before any GPU spend for the two safety cases."""

    def _run_main(self, rp, tmp_path, monkeypatch, job_spec, *, input_url, rf3, tier):
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        payload = {
            "job_spec": job_spec,
            "input_presigned_url": input_url,
            "upload_urls_endpoint": "https://example/upload",
            "job_token": "t",
            "tier": tier,
        }
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps(payload))
        monkeypatch.setenv("JOB_TIER", tier)
        monkeypatch.setenv("JOB_ID", "job-guard")
        monkeypatch.setenv("PROTEINA_RF3", rf3)
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        return json.loads(result_file.read_text())

    def test_custom_target_hard_block(self, tmp_path, monkeypatch):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_binder_local_pipeline", "task_name": "02_PDL1",
             "rf3_required": False, "nsamples": 4, "replicas": 2},
            input_url="https://example/target.pdb", rf3="on", tier="protein_binder")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "custom_target"

    def test_rf3_killswitch_hard_block(self, tmp_path, monkeypatch):
        rp = pytest.importorskip("tools.proteina.run_pipeline")
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_ligand_binder_local_pipeline",
             "task_name": "39_7V11_LIGAND", "rf3_required": True,
             "nsamples": 4, "replicas": 2},
            input_url="", rf3="off", tier="ligand_binder")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "rf3"


# ---------------------------------------------------------------------------
# 6. Template parses
# ---------------------------------------------------------------------------


class TestTemplatesParse:
    def test_results_template_parses(self):
        from pathlib import Path
        from jinja2 import Environment
        env = Environment()
        base = Path(__file__).resolve().parents[1] / "templates" / "tools"
        src = (base / "proteina_results.html").read_text(encoding="utf-8")
        env.parse(src)
