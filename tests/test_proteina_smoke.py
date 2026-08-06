"""Offline unit tests for the Proteina-Complexa tool.

Runs fully offline — no Modal, no Supabase, no GPU. Covers:

1. Adapter registration (slug, 4 presets, templates).
2. ``validate()`` per preset — config_name mapping, default task names (incl.
   the M-prefixed AME task, NOT the non-existent "01_AME"), rf3_required flags,
   and the reject cases.
3. Custom-target parsing: chain/residue contigs, chain-prefixed AND bare
   hotspots, binder length, and the curated-vs-custom exclusivity rules.
4. ``build_payload()`` shape.
5. Pricing + campaign wiring (TOOL_SPECS / PER_JOB_HARD_CAP_USD / PRESET_CAPS /
   chunk size / fixed-container / launch concurrency / first-wave hold).
6. ``run_pipeline`` pure helpers — deterministic distinct seeds, the RF3
   kill-switch env parse, the design CLI overrides, the tolerant reward-CSV
   parser + PDB matching against a synthetic run dir (the output-layer guard),
   and the custom-target structure verification.
7. Pre-GPU guards: the target-source invariant and the hotspot-existence check
   that stops a silently-unconstrained search.
8. Templates parse.
9. IMAGE REPRODUCIBILITY. Instruction-level guards on ``Dockerfile.modal`` —
   the dm-haiku pin, the runtime-dep install, the venv's place on ``PATH``, the
   build-time import gate, that neither guard's exit status is discarded, and
   that nothing installs a Python package below the pin or runs below the gate
   — plus AST/subprocess guards on the local Modal entrypoints. These are the
   only offline way to stop a guard whose whole value is that it exists from
   being quietly tidied away; the thing they protect is only observable in a
   real image build.

THE FAILURE THIS FILE EXISTS TO PIN. Upstream's ``load_target_from_pdb``
matches hotspots with ``f"{atom.chain_id}{atom.res_id}" in target_hotspots``
against a zero-initialised mask: a token matching nothing is dropped SILENTLY,
the search runs unconstrained, and the output is indistinguishable from a
correct run. Several tests below look pedantic (case sensitivity, argv element
splitting, MSE records) and are not — each one is a distinct way that silent
drop can happen.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import proteina as px
from tools.base import get as get_adapter
# Imported for real, not via pytest.importorskip. Two guards used to stand here
# and both were silent-skip hazards on the tests that matter most:
#   * importorskip("yaml") -- PyYAML was in nobody's requirements file, so the
#     ten targets_dict.yaml regression tests (the nesting bug: upstream nests
#     records under `target_dict_cfg:`) reported SKIPPED in every environment,
#     including CI. They had never once run. PyYAML is now declared in
#     requirements-dev.txt so a missing copy is an error, not a shrug.
#   * importorskip("tools.proteina.run_pipeline") -- the module under test.
#     It imports stdlib plus `requests` (in requirements.txt), so there is no
#     environment where skipping is the right answer; any ImportError here is a
#     defect these tests exist to catch, and it would have silenced all 51.
from tools.proteina import run_pipeline as rp


# A tiny two-chain structure with the awkward cases baked in: an MSE HETATM
# (biotite counts it as protein, an ATOM-only parser would not), a water HETATM
# (never a residue), an insertion-coded twin, and a second model after ENDMDL.
FIXTURE_PDB = """\
ATOM      1  N   ALA A  10       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A  10       1.000   0.000   0.000  1.00  0.00           C
ATOM      3  CA  GLY A  11       2.000   0.000   0.000  1.00  0.00           C
HETATM    4  CA  MSE A  12       3.000   0.000   0.000  1.00  0.00           C
ATOM      5  CA  VAL A  13       4.000   0.000   0.000  1.00  0.00           C
ATOM      6  CA  LEU A  13A      4.500   0.000   0.000  1.00  0.00           C
HETATM    7  CA  HOH A  99       9.000   0.000   0.000  1.00  0.00           C
ATOM      8  CA  SER B   5       0.000   1.000   0.000  1.00  0.00           C
ATOM      9  CA  THR B   6       0.000   2.000   0.000  1.00  0.00           C
ENDMDL
ATOM     10  CA  TRP C  77       0.000   0.000   9.000  1.00  0.00           C
"""


def _make_pdb(spans, extra=""):
    """A structure big enough to clear the pipeline's minimum-size gate.

    ``spans`` is {chain: (lo, hi)}. The tiny FIXTURE_PDB above is deliberately
    below that gate, so the end-to-end registration tests need a real one.
    """
    lines = []
    serial = 1
    for chain, (lo, hi) in spans.items():
        for resseq in range(lo, hi + 1):
            lines.append(
                f"ATOM  {serial:5d}  CA  ALA {chain}{resseq:4d}    "
                f"{serial * 1.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
            )
            serial += 1
    return "\n".join(lines) + "\n" + extra


def _atom(serial, atom, resname, chain, resseq, icode="", record="ATOM  "):
    """One coordinate line in the real column layout.

    Cols 1-6 record, 7-11 serial, 13-16 atom name, 18-20 resName, 22 chainID,
    23-26 resSeq, 27 iCode, 31-54 xyz. The crop reads columns 22, 23-26 and 27
    and copies the line verbatim, so a fixture built with sloppy columns would
    test a parser this code does not have.
    """
    return (
        f"{record}{serial:5d} {atom:<4s} {resname:>3s} {chain}{resseq:4d}"
        f"{icode:1s}   {serial * 1.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
    )


def _make_3s7g_like():
    """A stand-in for the campaign input that crashed, with its arithmetic.

    Chain A 236-443 (208 CA) and chain B 236-442 (207 CA) — the real spans of
    ``3S7G.pdb`` — plus the two chains the contig will not name, 20 waters at
    resid 1-20 (outside every range and carrying no CA, exactly as in the
    deposit), a HETATM ligand numbered INSIDE chain A's range, and the
    annotation records that describe residues the crop removes.

    Each residue gets N, CA and C so the crop is exercised on more than the one
    atom the counting parser looks at.
    """
    lines, serial = [], 1
    for chain, lo, hi in (("A", 236, 443), ("B", 236, 442),
                          ("C", 1, 90), ("D", 1, 90)):
        for resseq in range(lo, hi + 1):
            for atom in (" N", "CA", "C"):
                lines.append(_atom(serial, atom, "ALA", chain, resseq))
                serial += 1
        lines.append(f"TER   {serial:5d}      ALA {chain}{hi:4d}")
        serial += 1
    for resseq in range(1, 21):
        lines.append(_atom(serial, "O", "HOH", "A", resseq, record="HETATM"))
        serial += 1
    lines.append(_atom(serial, "C1", "GDP", "A", 500, record="HETATM"))
    serial += 1
    lines.append(_atom(serial, "C1", "GDP", "A", 300, record="HETATM"))
    lines.insert(0, "HEADER    IMMUNE SYSTEM")
    lines.insert(1, "SEQRES   1 A  208  ALA ALA ALA ALA ALA ALA ALA ALA")
    # An ANISOU on a residue the crop KEEPS, so its removal is a decision about
    # the record type rather than a side effect of the range filter.
    lines.insert(
        3, "ANISOU    2  CA  ALA A 236     1000   1000   1000      0      0"
           "      0       C")
    lines.append("CONECT    1    2")
    lines.append("END")
    return "\n".join(lines) + "\n"


def _custom(**form):
    """A form dict on the bring-your-own path. ``_has_custom_target`` is what
    the routes inject once they know a structure exists."""
    base = {"preset": "protein_binder", "_has_custom_target": "1"}
    base.update(form)
    return base


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
        assert a.form_template == "tools/proteina_form.html"

    def test_form_template_exists(self):
        """The adapter declared this path for a long time while the file did
        not exist — safe only because the campaign-only redirect fired first.
        Now that proteina has an atomic route, the file has to be real."""
        a = get_adapter("proteina")
        base = Path(__file__).resolve().parents[1] / "templates"
        assert (base / a.form_template).is_file()

    def test_not_campaign_only(self):
        """One shard IS a viable single container (8 designs, ~$15 hold against
        a $60 cap). proteina was campaign-only because it shipped no form, not
        because an atomic run is unviable."""
        from shared import compute_campaigns as cc
        assert "proteina" not in cc.CAMPAIGN_ONLY_TOOLS


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
        # Regression: the default AME task must be a real M-prefixed key whose
        # target PDB is git-bundled. Not "01_AME" (non-existent) and not the bare
        # "M0024_1nzy" (v2 key with no bundled target) — both would be billed GPU
        # failures. M0024_1nzy_og carries an explicit target_path to a bundled PDB.
        inp, err = px.validate({"preset": "motif_ame"}, {})
        assert err is None
        assert inp["config_name"] == "search_ame_local_pipeline"
        assert inp["task_name"] == "M0024_1nzy_og"
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

    def test_chain_id_too_long(self):
        _, err = px.validate(
            {"preset": "protein_binder", "task_name": "02_PDL1", "target_chain": "ABCDE"}, {})
        assert err and "chain" in err.lower()

    def test_multi_chain_is_accepted(self):
        """The old rule was len(target_chain) <= 4, which rejected "A B C" at
        five characters and made a multi-chain target unreachable — even though
        a three-chain target is a validated upstream example."""
        inp, err = px.validate(
            {"preset": "protein_binder", "task_name": "02_PDL1", "target_chain": "A B C"}, {})
        assert err is None and inp["target_chain"] == "A B C"


# ---------------------------------------------------------------------------
# 3. Custom targets: contigs, hotspots, binder length, exclusivity
# ---------------------------------------------------------------------------


class TestTargetInputParse:
    def test_single_chain_range(self):
        inp, err = px.validate(_custom(target_input="A1-150"), {})
        assert err is None
        assert inp["target_input"] == "A1-150"
        assert inp["target_chain"] == "A"

    def test_multi_chain_contig_derives_the_chain_field(self):
        """The contig names its own chains, so target_chain is derived from it
        rather than read separately — one source of truth, and it still feeds
        the routes' DesignTarget.chain_error range check."""
        inp, err = px.validate(
            _custom(target_input="A12-157,B12-157,C12-157", target_chain="Z"), {})
        assert err is None
        assert inp["target_input"] == "A12-157,B12-157,C12-157"
        assert inp["target_chain"] == "A B C"

    def test_bare_chain_means_whole_chain(self):
        inp, err = px.validate(_custom(target_input="B"), {})
        assert err is None and inp["target_chain"] == "B"

    def test_backwards_range_rejected(self):
        _, err = px.validate(_custom(target_input="A150-1"), {})
        assert err and "backwards" in err.lower()

    def test_duplicate_chain_rejected(self):
        _, err = px.validate(_custom(target_input="A1-50,A60-90"), {})
        assert err and "more than once" in err.lower()

    def test_garbage_rejected(self):
        _, err = px.validate(_custom(target_input="not-a-range"), {})
        assert err and "not valid" in err.lower()

    def test_segments_are_exposed_for_the_route_check(self):
        """Underscore-prefixed so sanitize_shared_params drops it from
        campaign.params — the container re-derives segments from the contig."""
        inp, err = px.validate(_custom(target_input="A1-150,B2-40"), {})
        assert err is None
        assert inp["_target_segments"] == [("A", 1, 150), ("B", 2, 40)]


class TestHotspotParse:
    def test_chain_prefixed(self):
        inp, err = px.validate(_custom(target_input="A1-150", hotspot_residues="A45 A67 A89"), {})
        assert err is None
        assert inp["hotspot_spec"] == ["A45", "A67", "A89"]
        assert inp["hotspot_residues"] == [45, 67, 89]

    def test_bare_ints_from_the_shared_launch_field(self):
        """The one shared hotspot field on the multi-tool launch screen posts
        "42,88" for every tool (tests/test_target_multi_launch_routes.py). A
        chain-prefixed-only parser would make proteina un-co-launchable with
        rfdiffusion/pxdesign, so bare numbers promote onto the target chain."""
        inp, err = px.validate(_custom(target_chain="A", hotspot_residues="42,88"), {})
        assert err is None
        assert inp["hotspot_spec"] == ["A42", "A88"]
        assert inp["hotspot_residues"] == [42, 88]

    def test_comma_and_space_both_work(self):
        a, _ = px.validate(_custom(target_input="A1-150", hotspot_residues="A45,A67"), {})
        b, _ = px.validate(_custom(target_input="A1-150", hotspot_residues="A45 A67"), {})
        assert a["hotspot_spec"] == b["hotspot_spec"] == ["A45", "A67"]

    def test_multi_chain_hotspots(self):
        inp, err = px.validate(
            _custom(target_input="A12-157,C12-157", hotspot_residues="A113 C73"), {})
        assert err is None and inp["hotspot_spec"] == ["A113", "C73"]

    def test_chain_not_in_the_contig_is_rejected(self):
        _, err = px.validate(_custom(target_input="A1-150", hotspot_residues="B45"), {})
        assert err and "chain B" in err

    def test_malformed_token_rejected(self):
        _, err = px.validate(_custom(target_input="A1-150", hotspot_residues="A4x5"), {})
        assert err and "not valid" in err.lower()

    def test_empty_is_allowed(self):
        """Hotspots are OPTIONAL for proteina — an unconstrained search is a
        legitimate run (boltzgen's shape, not rfdiffusion's)."""
        inp, err = px.validate(_custom(target_input="A1-150"), {})
        assert err is None and inp["hotspot_spec"] == [] and inp["hotspot_residues"] == []

    def test_duplicates_collapse(self):
        inp, err = px.validate(_custom(target_chain="A", hotspot_residues="45,45,A45"), {})
        assert err is None and inp["hotspot_spec"] == ["A45"]

    def test_too_many_rejected(self):
        many = ",".join(str(i) for i in range(1, 100))
        _, err = px.validate(_custom(target_chain="A", hotspot_residues=many), {})
        assert err and "too many" in err.lower()


class TestBinderLength:
    def test_default_is_upstreams(self):
        inp, err = px.validate(_custom(target_input="A1-150"), {})
        assert err is None and inp["binder_length"] == [60, 120]

    def test_explicit(self):
        inp, err = px.validate(
            _custom(target_input="A1-150", binder_length_min="50", binder_length_max="90"), {})
        assert err is None and inp["binder_length"] == [50, 90]

    def test_inverted_rejected(self):
        _, err = px.validate(
            _custom(target_input="A1-150", binder_length_min="120", binder_length_max="60"), {})
        assert err and "maximum" in err.lower()

    def test_out_of_envelope_rejected(self):
        _, err = px.validate(
            _custom(target_input="A1-150", binder_length_min="5", binder_length_max="900"), {})
        assert err and "between" in err.lower()


class TestTargetSourceExclusivity:
    def test_curated_by_default(self):
        inp, err = px.validate({"preset": "protein_binder"}, {})
        assert err is None and inp["target_source"] == "curated"

    def test_custom_when_declared(self):
        inp, err = px.validate(_custom(target_input="A1-150"), {})
        assert err is None and inp["target_source"] == "custom"

    def test_custom_with_a_blank_task_does_not_inherit_the_curated_default(self):
        """THE TRAP. The default-fill sits one line above the exclusivity check:
        leaving it unconditional stamps 02_PDL1 onto every bring-your-own run
        whose task field is blank — the normal case — and designs against PD-L1
        instead of the user's structure, on billed GPU, looking successful."""
        inp, err = px.validate(_custom(target_input="A1-150"), {})
        assert err is None
        assert inp["task_name"] == ""

    def test_custom_plus_curated_task_is_refused(self):
        _, err = px.validate(_custom(task_name="02_PDL1"), {})
        assert err and "mutually exclusive" in err.lower()

    def test_curated_plus_hotspots_is_refused(self):
        """A curated task carries its own hotspots and ++generation.task_name
        cannot override them, so accepting these would discard what was typed."""
        _, err = px.validate({"preset": "protein_binder", "hotspot_residues": "A45"}, {})
        assert err and "curated benchmark task" in err.lower()

    def test_curated_plus_contig_is_refused(self):
        _, err = px.validate({"preset": "protein_binder", "target_input": "A1-150"}, {})
        assert err and "curated benchmark task" in err.lower()

    def test_custom_refused_for_ligand_and_motif(self):
        """`complexa target add` writes configs/targets/targets_dict.yaml, which
        only the binder pipeline composes. The ligand and AME variants index
        separate registries the CLI cannot write."""
        for preset in ("ligand_binder", "motif_ame"):
            _, err = px.validate({"preset": preset, "_has_custom_target": "1"}, {})
            assert err and "cannot design against your own target" in err.lower(), preset


# ---------------------------------------------------------------------------
# 4. build_payload
# ---------------------------------------------------------------------------


_PAYLOAD_KEYS = {
    "preset", "config_name", "task_name", "target_source", "target_chain",
    "target_input", "hotspot_residues", "hotspot_spec", "binder_length",
    "rf3_required", "nsamples", "replicas", "nsteps", "parameters",
}


class TestBuildPayload:
    def test_shape(self):
        inp, err = px.validate({"preset": "ligand_binder"}, {})
        assert err is None
        bp = px.build_payload(inp, "https://example/presigned")
        # Exact-set on purpose: catching drift IS the point. Update it
        # deliberately when the contract changes, never loosen it to a subset.
        assert set(bp) == _PAYLOAD_KEYS
        assert bp["parameters"]["n_designs_total"] == 8

    def test_custom_target_payload(self):
        inp, err = px.validate(
            _custom(target_input="A12-157,B12-157", hotspot_residues="A113 B73",
                    binder_length_min="50", binder_length_max="120"), {})
        assert err is None
        bp = px.build_payload(inp, "https://example/presigned")
        assert set(bp) == _PAYLOAD_KEYS
        assert bp["target_source"] == "custom"
        assert bp["task_name"] == ""
        assert bp["target_input"] == "A12-157,B12-157"
        assert bp["target_chain"] == "A B"
        assert bp["hotspot_spec"] == ["A113", "B73"]
        assert bp["binder_length"] == [50, 120]

    def test_legacy_params_replay_without_the_new_keys(self):
        """A campaign created before these keys existed replays its stored
        params through build_payload on every later wave. A bare [] lookup
        would strand it mid-drain with a KeyError; the defaults have to
        reproduce the old curated behaviour exactly."""
        legacy = {
            "preset": "protein_binder",
            "config_name": "search_binder_local_pipeline",
            "task_name": "02_PDL1",
            "target_chain": "A",
            "rf3_required": False,
            "nsamples": 4, "replicas": 2, "nsteps": 400,
            "parameters": {"n_designs_total": 8},
        }
        bp = px.build_payload(legacy, "")
        assert set(bp) == _PAYLOAD_KEYS
        assert bp["target_source"] == "curated"
        assert bp["target_input"] == ""
        assert bp["hotspot_spec"] == [] and bp["hotspot_residues"] == []
        assert bp["binder_length"] == [60, 120]


# ---------------------------------------------------------------------------
# 5. Pricing + campaign wiring
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
        a = rp.shard_seed("job-abc")
        b = rp.shard_seed("job-abc")
        c = rp.shard_seed("job-xyz")
        assert a == b and a != c
        assert 0 <= a < 1_000_000

    def test_empty_job_id(self):
        assert rp.shard_seed("") == 42


class TestRf3Switch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("PROTEINA_RF3", raising=False)
        assert rp._rf3_enabled() is True

    @pytest.mark.parametrize("val", ["off", "false", "0", "no", "OFF"])
    def test_off_values(self, monkeypatch, val):
        monkeypatch.setenv("PROTEINA_RF3", val)
        assert rp._rf3_enabled() is False


class TestDesignCmd:
    def test_overrides_present(self):
        cmd = rp.build_design_cmd(
            config_name="search_binder_local_pipeline", task_name="02_PDL1",
            seed=123, nsamples=4, replicas=2, nsteps=400, run_name="shard_x",
            rf3_on=True)
        # config path is passed RELATIVE (run_pipeline runs from cwd=/opt/proteina)
        assert "configs/search_binder_local_pipeline.yaml" in cmd
        assert "++seed=123" in cmd
        assert "++job_id=0" in cmd
        assert "++gen_njobs=1" in cmd
        assert "++generation.task_name=02_PDL1" in cmd
        assert "++generation.filter.delete_non_top_n_samples=false" in cmd
        assert any("filter_samples_limit" in c for c in cmd)
        # designs/shard pinned explicitly (4 x 2 = 8) so every variant matches
        # the campaign chunk_size regardless of its config default.
        assert "++generation.dataloader.dataset.nres.nsamples=4" in cmd
        assert "++generation.search.best_of_n.replicas=2" in cmd
        # RF3 is config-gated (rf3folding in reward_models), never a CLI flag.
        assert not any("use_rf3" in c for c in cmd)

    def test_rf3_off_emits_no_toggle(self):
        # RF3 is enabled/disabled by whether rf3folding is present in the config's
        # reward_models block, NOT by a flag, so build_design_cmd emits no use_rf3
        # override in either state. The RF3-only variants (ligand/motif) are
        # hard-blocked in main() when PROTEINA_RF3=off — verified in TestPreGpuGuards.
        cmd = rp.build_design_cmd(
            config_name="search_binder_local_pipeline", task_name="02_PDL1",
            seed=1, nsamples=4, replicas=2, nsteps=None, run_name="s", rf3_on=False)
        assert not any("use_rf3" in c for c in cmd)
        assert "configs/search_binder_local_pipeline.yaml" in cmd


class TestRewardParse:
    def _make_run(self, tmp_path):
        # Synthetic reward CSV using the REAL verified PROTEIN column names
        # (af2folding_* + pdb_path), matching the P-2 canary output @916eaaed.
        # protein total_reward is NEGATIVE (== -i_pae), so higher is better.
        run = tmp_path / "run"
        sub = run / "inference" / "search_binder_local_pipeline_02_PDL1_s"
        sub.mkdir(parents=True)
        (sub / "design_A.pdb").write_text("ATOM\n")
        (sub / "design_B.pdb").write_text("ATOM\n")
        csv_text = (
            "pdb_path,pdb_index,total_reward,af2folding_i_ptm_log,af2folding_plddt,af2folding_rmsd,sample_type,metadata_tag\n"
            f"{sub / 'design_A.pdb'},0,-0.60,0.18,0.62,5.2,final,design_A\n"
            f"{sub / 'design_B.pdb'},1,-0.45,0.30,0.71,0.8,final,design_B\n"
        )
        (run / "inference" / "rewards_search_binder_local_pipeline_0.csv").write_text(csv_text)
        return run

    def test_parse_and_rank(self, tmp_path):
        run = self._make_run(tmp_path)
        designs = rp.parse_designs(run)
        assert len(designs) == 2
        # ranked by total_reward desc -> design_B (-0.45) beats design_A (-0.60)
        assert designs[0]["name"] == "design_B"
        assert designs[0]["rank"] == 0
        s = designs[0]["scores"]
        assert s["total_reward"] == -0.45
        assert s["af2_iptm"] == 0.30       # af2folding_i_ptm_log
        assert s["af2_plddt"] == 0.71      # af2folding_plddt
        assert s["binder_scrmsd"] == 0.8   # af2folding_rmsd
        assert s["rf3_score"] is None      # protein reward has no rf3 column
        assert s["cluster_id"] is None     # diversity assigned at the hub

    def test_ligand_columns_map(self, tmp_path):
        # Ligand reward CSV uses rf3folding_* names (P-3 canary). Verify the
        # tolerant mapping picks them up for the same display keys.
        run = tmp_path / "lig"
        (run / "inference").mkdir(parents=True)
        csv_text = (
            "pdb_path,total_reward,rf3folding_ipTM,rf3folding_plddt,rf3folding_ranking_score,metadata_tag\n"
            "b.pdb,0.87,0.86,0.85,0.868,lig_B\n"
        )
        (run / "inference" / "rewards_search_ligand_binder_local_pipeline_0.csv").write_text(csv_text)
        s = rp.parse_designs(run)[0]["scores"]
        assert s["af2_iptm"] == 0.86       # rf3folding_ipTM
        assert s["af2_plddt"] == 0.85      # rf3folding_plddt
        assert s["rf3_score"] == 0.868     # rf3folding_ranking_score
        assert s["binder_scrmsd"] is None  # ligand has no rmsd column

    def test_pdb_match(self, tmp_path):
        run = self._make_run(tmp_path)
        designs = rp.parse_designs(run)
        top = designs[0]
        pdb = rp.find_pdb_for(top, run, top["_row_index"], len(designs))
        assert pdb is not None and pdb.name == "design_B.pdb"

    def test_no_csv_returns_empty(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert rp.parse_designs(empty) == []


class TestStructureVerification:
    """The in-container checks that make a silently-ignored hotspot impossible.

    The container is standalone (modal_app copies one file), so none of this
    can reuse shared/pdb_inspect.py — these are hand-written stdlib parsers and
    they have to match upstream's behaviour exactly.
    """

    def _pdb(self, tmp_path):
        p = tmp_path / "t.pdb"
        p.write_text(FIXTURE_PDB)
        return p

    def test_ca_parse(self, tmp_path):
        res, bad = rp.pdb_ca_residues(self._pdb(tmp_path))
        assert bad == 0
        assert ("A", 12, "") in res, "MSE HETATM must count as protein"
        assert ("A", 99, "") not in res, "water must not"
        assert not any(r[0] == "C" for r in res), "parsing must stop at ENDMDL"
        assert ("B", 5, "") in res and ("B", 6, "") in res

    def test_mse_is_kept(self, tmp_path):
        """biotite treats MSE as protein when building the CA structure, so an
        ATOM-only parser would report a legitimate MSE hotspot as missing and
        refuse a valid run."""
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        sel = rp.select_residues(res, rp.parse_target_input("A10-13"))
        assert rp.missing_hotspots(sel, ["A12"]) == []

    def test_missing_hotspot_is_detected(self, tmp_path):
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        sel = rp.select_residues(res, rp.parse_target_input("A10-13"))
        assert rp.missing_hotspots(sel, ["A9999"]) == ["A9999"]

    def test_hotspot_matching_is_case_sensitive(self, tmp_path):
        """Upstream compares f"{chain_id}{res_id}" literally, so "a45" against
        an A45 residue is a miss THERE and must be a miss here — otherwise we
        wave through the run upstream then silently unconstrains."""
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        sel = rp.select_residues(res, rp.parse_target_input("A10-13"))
        assert rp.missing_hotspots(sel, ["a10"]) == ["a10"]

    def test_hotspot_on_another_chain_is_detected(self, tmp_path):
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        sel = rp.select_residues(res, rp.parse_target_input("A10-13"))
        assert rp.missing_hotspots(sel, ["B5"]) == ["B5"]

    def test_derive_segments_covers_every_chain(self, tmp_path):
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        assert rp.derive_segments(res, ["A", "B"]) == [("A", 10, 13), ("B", 5, 6)]
        assert rp.format_contig(rp.derive_segments(res, ["A", "B"])) == "A10-13,B5-6"

    def test_absent_chain_selects_nothing(self, tmp_path):
        """The refusal that makes atomworks' unverified from_contig behaviour
        irrelevant: we compute the selection ourselves and an empty one fails."""
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        assert rp.select_residues(res, rp.parse_target_input("Z1-99")) == []

    def test_out_of_range_selects_nothing(self, tmp_path):
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        assert rp.select_residues(res, rp.parse_target_input("A500-600")) == []

    def test_insertion_codes_are_reported_ambiguous(self, tmp_path):
        """A13 and A13A collapse to the same upstream match key, so a hotspot
        on one also constrains the other. Warned about, never fatal."""
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        assert rp.ambiguous_insertion_codes(res) == ["A13"]

    def test_multi_chain_contig_round_trip(self, tmp_path):
        res, _ = rp.pdb_ca_residues(self._pdb(tmp_path))
        sel = rp.select_residues(res, rp.parse_target_input("A10-13,B5-6"))
        assert rp.missing_hotspots(sel, ["A12", "B5"]) == []

    def test_a_resnum_at_or_above_10000_is_MISPARSED_not_counted_unparsable(
            self, tmp_path):
        """A DEFECT, CHARACTERISED - not endorsed, and not fixed here.

        ``pdb_ca_residues``'s docstring used to claim that "columns 22:26
        overflow at residue numbers >= 10000" and that the resulting residue is
        counted in ``n_unparsable`` and skipped. Both halves are false, and the
        docstring now says so; this test is what makes that correction checkable
        rather than a second unverified claim in place of the first.

        A residue numbered 10000 occupies columns 23-27, so ``line[22:26]``
        reads "1000": an int, no ValueError, nothing counted, nothing skipped -
        a SILENT MISPARSE onto a different residue number that no caller can
        currently detect.

        Left unfixed deliberately: a correct fix has to decide what a 5-column
        resSeq means, and PDB has no legal answer (hybrid-36 and the mmCIF
        convention disagree), which is a change to what counts as a residue
        rather than a docstring correction. If someone later fixes it, this test
        SHOULD fail - read the docstring before changing the expectation.
        """
        text = "\n".join(
            _atom(i + 1, "CA", "ALA", "A", r)
            for i, r in enumerate(range(9995, 10010))) + "\nEND\n"
        path = tmp_path / "big.pdb"
        path.write_text(text)
        residues, n_unparsable = rp.pdb_ca_residues(path)
        assert n_unparsable == 0, "the overflow does NOT raise, so nothing counts it"
        assert [r[1] for r in residues] == [9995, 9996, 9997, 9998, 9999] + [1000] * 10
        # Worse than one collision: the 5th digit lands in the INSERTION-CODE
        # column, so 10000..10009 come back as ten residues all numbered 1000,
        # told apart only by an "insertion code" of "0".."9".
        assert [r[2] for r in residues[5:]] == list("0123456789")
        # ...and the reason it does not break the crop: the keep key and the
        # count are built from the same wrong number, so they agree with each
        # other. That is containment, not correctness - upstream's assertion
        # holds while every one of these residues has the wrong id.
        segments = rp.parse_target_input("A1000-1000")
        keep = rp.selected_residue_keys(residues, segments)
        cropped = tmp_path / "c.pdb"
        cropped.write_text(rp.crop_pdb_to_contig(text, keep))
        staged, _ = rp.pdb_ca_residues(cropped)
        assert len(staged) == len(rp.select_residues(residues, segments)) == 10


class TestTargetAddCmd:
    """`complexa target add` argv. Each assertion here is a distinct silent
    failure mode, not style."""

    def _cmd(self, rp, **kw):
        base = dict(
            key="hub_0123456789abcdef", pdb_path="/opt/proteina/hub_targets/x.pdb",
            filename_stem="x", contig="A1-150", hotspot_spec=["A45", "A67"],
            binder_length=[60, 120],
        )
        base.update(kw)
        return rp.build_target_add_cmd(**base)

    def test_hotspots_are_separate_argv_elements(self, tmp_path):
        """--hotspot-residues is argparse nargs="+". Joined into one string,
        argparse takes "A45 A67" as a single token, it matches no residue, and
        upstream drops it to an all-zero mask without complaining. This is the
        single most likely silent bug in the whole path."""
        cmd = self._cmd(rp)
        i = cmd.index("--hotspot-residues")
        assert cmd[i + 1:i + 3] == ["A45", "A67"]

    def test_binder_length_is_two_argv_elements(self, tmp_path):
        cmd = self._cmd(rp)
        i = cmd.index("--binder-length")
        assert cmd[i + 1:i + 3] == ["60", "120"]

    def test_force_is_always_passed(self, tmp_path):
        """Without --force an existing key prompts input("Overwrite? (y/N): "),
        which EOFErrors on a container's closed stdin and returns False — a
        registration that silently did not happen. Warm containers reuse the
        filesystem, so the key CAN already be there."""
        assert "--force" in self._cmd(rp)

    def test_dict_path_is_explicit(self, tmp_path):
        """Upstream's get_default_dict_path() walks up from the cwd and falls
        back to a legacy configs/generation/ path. Naming the file removes a
        whole class of wrote-the-wrong-registry failure."""
        cmd = self._cmd(rp)
        assert "--dict" in cmd
        assert cmd[cmd.index("--dict") + 1].endswith("configs/targets/targets_dict.yaml")

    def test_target_input_is_never_omitted(self, tmp_path):
        """Omitted, upstream defaults it to "A1-100" — silently cropping a
        larger target to its first 100 residues."""
        cmd = self._cmd(rp, contig="A12-157,B12-157")
        assert cmd[cmd.index("--target-input") + 1] == "A12-157,B12-157"

    def test_no_interactive_or_pdb_id_flags(self, tmp_path):
        cmd = self._cmd(rp)
        for flag in ("-i", "--interactive", "-e", "--editor", "--pdb-id",
                     "--ligand", "--smiles"):
            assert flag not in cmd

    def test_no_hotspot_flag_when_none_requested(self, tmp_path):
        assert "--hotspot-residues" not in self._cmd(rp, hotspot_spec=[])

    def test_key_satisfies_the_adapter_task_regex(self, tmp_path):
        """task_name becomes ++generation.task_name, and the adapter bounds it
        to _TASK_RE — a key that fails it could never be selected."""
        key = rp.custom_target_key("job-1", "a" * 64, {"target_input": "A1-150"})
        assert px._TASK_RE.match(key)
        assert key.startswith("hub_")

    def test_key_is_stable_and_distinct(self, tmp_path):
        rec = {"target_input": "A1-150"}
        assert rp.custom_target_key("job-1", "s", rec) == rp.custom_target_key("job-1", "s", rec)
        assert rp.custom_target_key("job-1", "s", rec) != rp.custom_target_key("job-2", "s", rec)
        assert rp.custom_target_key("job-1", "s", rec) != rp.custom_target_key("job-1", "t", rec)

    def test_key_never_collides_with_a_curated_task(self, tmp_path):
        curated = set(px._DEFAULT_TASK.values())
        assert rp.custom_target_key("j", "s", {}) not in curated


class TestRegistrationReadback:
    """`add_target_cli` can return False WITHOUT a nonzero exit, so a clean rc
    proves nothing. The written record is the only trustworthy evidence.

    Only the pure half is tested — read_targets_dict imports PyYAML lazily
    because it ships with OmegaConf in the container image but is not a
    tools-hub dependency, so the module stays importable offline.
    """

    # The REAL file shape, copied from configs/targets/targets_dict.yaml at the
    # pinned commit: every record is nested one level down under a top-level
    # `target_dict_cfg:` key. Reading the outer mapping makes every lookup miss,
    # which turns a SUCCESSFUL `complexa target add` into "target was not
    # written to the registry" and fails every custom-target shard. The original
    # implementation did exactly that, and no test caught it because the
    # fixtures were hand-written flat dicts.
    UPSTREAM_SHAPE = """\
target_dict_cfg:
  02_PDL1:
    source: bindcraft_targets
    target_filename: PD-L1
    target_path: ./assets/target_data/bindcraft_targets/PD-L1.pdb
    target_input: A1-115
    hotspot_residues: ["A37", "A39", "A49", "A98"]
    binder_length: [64, 155]
    pdb_id: null

  hub_0123456789abcdef:
    source: tools_hub_upload
    target_filename: hub_x
    target_path: /opt/proteina/hub_targets/hub_x.pdb
    target_input: A1-150
    hotspot_residues: ["A45"]
    binder_length: [60, 120]
    pdb_id: null
"""

    def test_read_targets_dict_unwraps_the_nested_container(self, tmp_path):
        """Regression for the shape above. Upstream's own target_manager does
        `data.get("target_dict_cfg", data)` before indexing by name."""
        p = tmp_path / "targets_dict.yaml"
        p.write_text(self.UPSTREAM_SHAPE)
        records = rp.read_targets_dict(str(p))
        assert "hub_0123456789abcdef" in records, "records must be unwrapped"
        assert "02_PDL1" in records
        assert "target_dict_cfg" not in records

    def test_readback_accepts_a_record_written_in_the_real_shape(self, tmp_path):
        """The end-to-end version: a correctly registered target must verify."""
        p = tmp_path / "targets_dict.yaml"
        p.write_text(self.UPSTREAM_SHAPE)
        records = rp.read_targets_dict(str(p))
        assert rp.registration_mismatch(
            records.get("hub_0123456789abcdef"), self._expected()) is None

    def test_curated_collision_guard_can_actually_fire(self, tmp_path):
        """The guard reads the same registry; nested, it could never see a
        curated key and so could never refuse a collision."""
        p = tmp_path / "targets_dict.yaml"
        p.write_text(self.UPSTREAM_SHAPE)
        records = rp.read_targets_dict(str(p))
        prior = records.get("02_PDL1")
        assert isinstance(prior, dict)
        assert str(prior.get("source")) != rp._HUB_SOURCE

    def test_flat_shape_still_readable(self, tmp_path):
        """The legacy configs/generation/ layout has no wrapper."""
        p = tmp_path / "flat.yaml"
        p.write_text("hub_x:\n  source: tools_hub_upload\n")
        assert "hub_x" in rp.read_targets_dict(str(p))

    def _expected(self):
        return {
            "source": "tools_hub_upload",
            "target_path": "/opt/proteina/hub_targets/hub_x.pdb",
            "target_input": "A1-150",
            "hotspot_residues": ["A45"],
            "binder_length": [60, 120],
        }

    def test_match_returns_none(self):
        assert rp.registration_mismatch(dict(self._expected()), self._expected()) is None

    def test_absent_record_is_a_mismatch(self):
        assert rp.registration_mismatch(None, self._expected())

    def test_wrong_target_path_is_a_mismatch(self):
        rec = dict(self._expected(), target_path="/somewhere/else.pdb")
        assert "target_path" in rp.registration_mismatch(rec, self._expected())

    def test_dropped_hotspots_are_a_mismatch(self):
        rec = dict(self._expected(), hotspot_residues=[])
        assert "hotspot_residues" in rp.registration_mismatch(rec, self._expected())

    def test_wrong_contig_is_a_mismatch(self):
        rec = dict(self._expected(), target_input="A1-100")
        assert "target_input" in rp.registration_mismatch(rec, self._expected())


class TestPreGpuGuards:
    """main() must FAIL before any GPU spend for every unsafe target state."""

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

    def test_staged_target_on_an_undeclared_run_is_refused(self, tmp_path, monkeypatch):
        """The original bring-your-own hard block, narrowed but intact. A staged
        structure arriving on a run that did NOT declare a custom target must
        never fall through to ++generation.task_name, which resolves a
        repo-bundled benchmark target — that designs against the wrong
        structure on billed GPU and looks entirely successful."""
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_binder_local_pipeline", "task_name": "02_PDL1",
             "rf3_required": False, "nsamples": 4, "replicas": 2},
            input_url="https://example/target.pdb", rf3="on", tier="protein_binder")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_conflict"

    def test_declared_custom_without_a_url_is_refused(self, tmp_path, monkeypatch):
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_binder_local_pipeline", "task_name": "",
             "target_source": "custom", "target_chain": "A",
             "rf3_required": False, "nsamples": 4, "replicas": 2},
            input_url="", rf3="on", tier="protein_binder")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_missing"

    def test_custom_carrying_a_curated_task_is_refused(self, tmp_path, monkeypatch):
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_binder_local_pipeline", "task_name": "02_PDL1",
             "target_source": "custom", "target_chain": "A",
             "rf3_required": False, "nsamples": 4, "replicas": 2},
            input_url="https://example/target.pdb", rf3="on", tier="protein_binder")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_conflict"

    def test_custom_on_a_non_binder_variant_is_refused(self, tmp_path, monkeypatch):
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_ame_local_pipeline", "task_name": "",
             "target_source": "custom", "target_chain": "A",
             "rf3_required": False, "nsamples": 4, "replicas": 2},
            input_url="https://example/target.pdb", rf3="on", tier="motif_ame")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "custom_target_variant"

    def test_no_task_and_no_custom_target_is_refused(self, tmp_path, monkeypatch):
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_binder_local_pipeline", "task_name": "",
             "rf3_required": False, "nsamples": 4, "replicas": 2},
            input_url="", rf3="on", tier="protein_binder")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_missing"

    def test_rf3_killswitch_hard_block(self, tmp_path, monkeypatch):
        data = self._run_main(
            rp, tmp_path, monkeypatch,
            {"config_name": "search_ligand_binder_local_pipeline",
             "task_name": "39_7V11_LIGAND", "rf3_required": True,
             "nsamples": 4, "replicas": 2},
            input_url="", rf3="off", tier="ligand_binder")
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "rf3"


class TestCustomTargetRegistration:
    """End-to-end wiring of the custom-target path, with the network and the
    `complexa` binary stubbed. These run main() for real, so they also prove
    the ordering: verify, then register, then design."""

    def _drive(self, rp, tmp_path, monkeypatch, job_spec, *, calls, hotspot_spec=(),
               fail_registration=False, pdb_spans=None, pdb_text=None):
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(tmp_path / "raw.tgz"))
        # Keep every filesystem effect inside tmp_path.
        home = tmp_path / "proteina"
        (home / "configs" / "targets").mkdir(parents=True)
        registry = home / "configs" / "targets" / "targets_dict.yaml"
        # The REAL upstream shape: records nested under `target_dict_cfg:`.
        # A flat fixture here would make these end-to-end tests pass against a
        # registry layout that does not exist, which is exactly how the nesting
        # bug survived the first round of tests.
        registry.write_text(
            "target_dict_cfg:\n"
            "  02_PDL1:\n"
            "    source: bindcraft_targets\n"
            "    target_path: ./assets/target_data/bindcraft_targets/PD-L1.pdb\n"
        )
        monkeypatch.setattr(rp, "PROTEINA_HOME", str(home))
        monkeypatch.setattr(rp, "_TARGETS_DICT", str(registry))
        monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(home / "hub_targets"))

        spans = pdb_spans or {"A": (1, 60), "B": (1, 40)}

        def fake_download(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(pdb_text if pdb_text is not None else _make_pdb(spans))
            return dest
        monkeypatch.setattr(rp, "download_target", fake_download)

        def fake_run(cmd, cwd):
            calls.append(list(cmd))
            if cmd[:3] == [rp.COMPLEXA_BIN, "target", "add"] and not fail_registration:
                key = cmd[3]
                import yaml
                data = yaml.safe_load(registry.read_text()) or {}
                # Upstream appends INSIDE target_dict_cfg (_format_target_entry
                # emits the record at 2-space indent), so the stub must too.
                data.setdefault("target_dict_cfg", {})
                data["target_dict_cfg"][key] = {
                    "source": rp._HUB_SOURCE,
                    "target_path": cmd[cmd.index("--target-path") + 1],
                    "target_input": cmd[cmd.index("--target-input") + 1],
                    "hotspot_residues": list(hotspot_spec),
                    "binder_length": [
                        int(cmd[cmd.index("--binder-length") + 1]),
                        int(cmd[cmd.index("--binder-length") + 2]),
                    ],
                }
                registry.write_text(yaml.safe_dump(data, sort_keys=False))
            return 0
        monkeypatch.setattr(rp, "run_streaming", fake_run)

        payload = {
            "job_spec": job_spec,
            "input_presigned_url": "https://example/target.pdb",
            "upload_urls_endpoint": "https://example/upload",
            "job_token": "t", "tier": "protein_binder",
        }
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps(payload))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-custom")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        try:
            rp.main()
        except SystemExit:
            pass
        return json.loads(result_file.read_text())

    def _spec(self, **kw):
        base = {
            "config_name": "search_binder_local_pipeline", "task_name": "",
            "target_source": "custom", "target_chain": "A",
            "target_input": "A1-60", "hotspot_spec": [], "binder_length": [60, 120],
            "rf3_required": False, "nsamples": 4, "replicas": 2,
        }
        base.update(kw)
        return base

    def test_a_tagged_target_is_refused_before_any_subprocess(self, tmp_path, monkeypatch):
        """The derived-contig half of the negative-numbering guard. The user
        gave no explicit range, so derive_segments() takes the chain's full
        observed span — which on an expression-tagged construct starts below
        zero and renders as "A-5-240". Upstream's CONTIG_REGEX cannot match it,
        so `complexa design` raises ValueError. Nothing may run."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="", target_chain="A"),
            calls=calls, pdb_spans={"A": (-5, 240)})
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input_negative"
        assert calls == [], "nothing may execute once the contig is unrenderable"
        # The message has to carry the fix, not just the complaint.
        assert "A0-240" in data["error"]["detail"]

    def test_the_suggested_range_actually_works(self, tmp_path, monkeypatch):
        """Companion to the test above: the contig the refusal recommends must
        register and design cleanly on the same structure. A guard that refuses
        with unusable advice is a dead end, not a gate."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="A0-240", target_chain="A"),
            calls=calls, pdb_spans={"A": (-5, 240)})
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == "A0-240"

    def test_registers_then_designs_with_the_same_key(self, tmp_path, monkeypatch):
        """THE end-to-end wiring assertion: the key written to the registry is
        the key ++generation.task_name selects. If these ever drift, Hydra
        resolves a target we did not register."""
        calls: list = []
        self._drive(rp, tmp_path, monkeypatch,
                    self._spec(hotspot_spec=["A12", "A30"]), calls=calls,
                    hotspot_spec=["A12", "A30"])
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        design = next(c for c in calls if c[:2] == [rp.COMPLEXA_BIN, "design"])
        key = add[3]
        assert f"++generation.task_name={key}" in design
        # And never a curated one.
        for curated in px._DEFAULT_TASK.values():
            assert f"++generation.task_name={curated}" not in design

    def test_registration_precedes_design(self, tmp_path, monkeypatch):
        calls: list = []
        self._drive(rp, tmp_path, monkeypatch, self._spec(), calls=calls)
        kinds = [c[1] for c in calls]
        assert kinds.index("target") < kinds.index("design")

    def test_a_missing_hotspot_fails_before_complexa_is_ever_run(self, tmp_path, monkeypatch):
        """The headline guard. Upstream would accept A9999, match nothing, and
        run an unconstrained search that looks exactly like a successful one.
        Nothing may be executed at all: not the registration, not the design."""
        calls: list = []
        data = self._drive(rp, tmp_path, monkeypatch,
                           self._spec(hotspot_spec=["A9999"]), calls=calls)
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "hotspot_missing"
        assert calls == [], "no subprocess may run once a hotspot is unmatched"

    def test_an_empty_chain_range_fails_before_complexa(self, tmp_path, monkeypatch):
        calls: list = []
        data = self._drive(rp, tmp_path, monkeypatch,
                           self._spec(target_input="Z1-99"), calls=calls)
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input"
        assert calls == []

    def test_a_silent_registration_failure_is_caught(self, tmp_path, monkeypatch):
        """`complexa target add` exits 0 but writes nothing (its overwrite
        prompt EOFs on a closed stdin and returns False). The read-back is what
        turns that into a refusal instead of a Hydra traceback mid-GPU."""
        calls: list = []
        data = self._drive(rp, tmp_path, monkeypatch, self._spec(),
                           calls=calls, fail_registration=True)
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_registration"
        assert not any(c[:2] == [rp.COMPLEXA_BIN, "design"] for c in calls)

    def test_a_curated_key_collision_is_refused(self, tmp_path, monkeypatch):
        calls: list = []
        real_key = rp.custom_target_key  # noqa: F841 — documents intent below

        # Force the derived key to collide with a curated entry already in the
        # registry and NOT written by us.
        monkeypatch.setattr(rp, "custom_target_key", lambda *a, **k: "02_PDL1")
        data = self._drive(rp, tmp_path, monkeypatch, self._spec(), calls=calls)
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_key_collision"
        assert calls == []


def _write(path, text):
    path.write_text(text)
    return path


class TestStagedTargetIsCroppedToTheContig:
    """THE INVARIANT UPSTREAM ASSERTS AND NEVER CHECKS.

    ``proteinfoundation/metrics/metric_utils.py`` (pinned 916eaaed) ends the
    evaluate stage with::

        assert (np.isin(gen_pdb.chain_id, gen_pdb_target_chain)).sum() == len(target_seq)

    Left: CA atoms of the target chains in the GENERATED complex, which holds
    only the contig's selection (``pdb_utils`` masks through
    ``AtomSelectionStack.from_contig``). Right: ``len(target_seq)``, built by
    ``binder_eval_utils`` from the STAGED file restricted to the chains the
    contig NAMES — ``sorted(set(x[0] for x in target_input.split(",")))``, the
    letters only, the ranges discarded.

    It compares COUNTS, never residue numbers, so upstream silently requires the
    contig to cover every CA residue of each chain it names. This wrapper used
    to stage the upload verbatim, so every sub-range contig died in evaluate
    after the GPU had generated and scored every design.

    Every test below is driven from a SUB-RANGE contig on a multi-chain input.
    A whole-chain case would have satisfied the invariant before this fix too
    and would prove nothing.
    """

    _CONTIG = "A236-300,B236-300"

    # Borrowed rather than inherited: subclassing TestCustomTargetRegistration
    # would re-run all of its tests under this name as well.
    _drive = TestCustomTargetRegistration._drive
    _spec = TestCustomTargetRegistration._spec

    def _parsed(self, tmp_path, text=None, name="in.pdb"):
        p = tmp_path / name
        p.write_text(text if text is not None else _make_3s7g_like())
        residues, _ = rp.pdb_ca_residues(p)
        return p, residues

    def _upstream_counts(self, residues, contig):
        """(left, right) of upstream's assertion for a file with ``residues``.

        ``named`` drops the ranges exactly as ``binder_eval_utils`` does.
        """
        segments = rp.parse_target_input(contig)
        named = {seg[0] for seg in segments}
        return (sum(1 for r in residues if r[0] in named),
                len(rp.select_residues(residues, segments)))

    def test_the_uncropped_upload_violates_the_assertion(self, tmp_path):
        """The premise. Without this the tests below could pass vacuously.

        These are the real 3S7G numbers: 208 CA in chain A, 207 in chain B, and
        a contig selecting 65 of each.
        """
        _p, residues = self._parsed(tmp_path)
        assert rp.chain_span_summary(residues) == "A236-443, B236-442, C1-90, D1-90"
        left, right = self._upstream_counts(residues, self._CONTIG)
        assert (left, right) == (415, 130)
        assert left != right, "the crash this fix exists to stop"

    def test_the_cropped_file_satisfies_the_assertion(self, tmp_path):
        """The one that matters, on the exact quantity upstream compares."""
        _p, residues = self._parsed(tmp_path)
        segments = rp.parse_target_input(self._CONTIG)
        cropped = tmp_path / "cropped.pdb"
        cropped.write_text(rp.crop_pdb_to_contig(
            (tmp_path / "in.pdb").read_text(),
            rp.selected_residue_keys(residues, segments)))
        staged_residues, unparsable = rp.pdb_ca_residues(cropped)
        assert unparsable == 0
        left, right = self._upstream_counts(staged_residues, self._CONTIG)
        assert left == right == 130
        # ...and the selection did not change while we cropped: the residues the
        # contig picks out of the cropped file are the ones it picked before.
        assert (rp.select_residues(staged_residues, segments)
                == rp.select_residues(residues, segments))

    def test_a_single_chain_sub_range_is_cropped_too(self, tmp_path):
        """It is NOT a multi-chain problem and NOT a non-1-based-numbering one.

        ``A1-115`` on a 1-based single chain of 208 residues crashes upstream
        for the same reason, so the crop may not be conditional on either.
        """
        text = "\n".join(
            _atom(i + 1, "CA", "ALA", "A", i + 1) for i in range(208)) + "\nEND\n"
        _p, residues = self._parsed(tmp_path, text)
        assert self._upstream_counts(residues, "A1-115") == (208, 115)
        segments = rp.parse_target_input("A1-115")
        out = tmp_path / "c.pdb"
        out.write_text(rp.crop_pdb_to_contig(
            text, rp.selected_residue_keys(residues, segments)))
        staged, _ = rp.pdb_ca_residues(out)
        assert self._upstream_counts(staged, "A1-115") == (115, 115)

    def test_author_numbering_survives_the_crop_unchanged(self, tmp_path):
        """THE thing the crop must never do.

        Upstream matches a hotspot as the literal string ``f"{chain_id}{res_id}"``
        and this wrapper's preflight is built on the same concatenation, so a
        crop that renumbered 236-300 to 1-65 would move every hotspot silently —
        the exact failure class the whole custom-target path exists to close.
        """
        _p, residues = self._parsed(tmp_path)
        segments = rp.parse_target_input(self._CONTIG)
        out = tmp_path / "c.pdb"
        out.write_text(rp.crop_pdb_to_contig(
            (tmp_path / "in.pdb").read_text(),
            rp.selected_residue_keys(residues, segments)))
        staged, _ = rp.pdb_ca_residues(out)
        assert rp.chain_span_summary(staged) == "A236-300, B236-300"
        assert [(c, r) for c, r, _i in staged][:3] == [
            ("A", 236), ("A", 237), ("A", 238)]
        # The hotspot keys are literally unchanged, which is the property the
        # span check above only implies.
        before = rp.hotspot_keys(rp.select_residues(residues, segments))
        assert rp.hotspot_keys(rp.select_residues(staged, segments)) == before
        assert "A236" in before and "B300" in before and "A301" not in before

    def test_chains_the_contig_does_not_name_are_dropped_entirely(self, tmp_path):
        """4-chain deposit, 2-chain contig. C and D contribute nothing to the
        right-hand side, but leaving them means the model is handed a structure
        the contig cannot describe."""
        _p, residues = self._parsed(tmp_path)
        segments = rp.parse_target_input(self._CONTIG)
        out = tmp_path / "c.pdb"
        out.write_text(rp.crop_pdb_to_contig(
            (tmp_path / "in.pdb").read_text(),
            rp.selected_residue_keys(residues, segments)))
        staged, _ = rp.pdb_ca_residues(out)
        assert sorted({r[0] for r in staged}) == ["A", "B"]
        for line in out.read_text().splitlines():
            if line[:6] in ("ATOM  ", "HETATM"):
                assert line[21:22] in ("A", "B"), line

    def test_waters_ligands_and_annotation_are_dropped(self, tmp_path):
        """The deliberate non-CA decision, including the case that is easy to
        miss: a ligand numbered INSIDE the range, sharing a residue number with
        a polymer residue the crop keeps."""
        _p, residues = self._parsed(tmp_path)
        segments = rp.parse_target_input(self._CONTIG)
        text = rp.crop_pdb_to_contig(
            (tmp_path / "in.pdb").read_text(),
            rp.selected_residue_keys(residues, segments))
        assert "HOH" not in text, "water"
        assert "GDP" not in text, "ligand at A300 is inside the range and must go"
        assert "HETATM" not in text
        assert "SEQRES" not in text, (
            "SEQRES declares the FULL chain sequence; any code deriving the "
            "target length from it would read the uncropped number back")
        assert "CONECT" not in text and "HEADER" not in text
        # ANISOU on a KEPT residue, so this is a decision about the record type,
        # not a side effect of the range filter. It goes because dropping it
        # alongside a rejected ligand would mean matching it back to its parent
        # atom, and a dangling ANISOU is worse than no ANISOU.
        assert "ANISOU" in (tmp_path / "in.pdb").read_text(), "fixture check"
        assert "ANISOU" not in text
        assert text.rstrip().endswith("END")
        assert text.count("\nTER   ") == 2, "one TER per surviving chain"
        # ...and only the two record types the crop declares it carries.
        assert {line[:6].strip() for line in text.splitlines()} == {
            "ATOM", "TER", "END"}

    def test_a_modified_residue_inside_the_range_survives(self, tmp_path):
        """The other side of the HETATM rule. MSE is protein to biotite AND to
        pdb_ca_residues, so dropping it would make our count disagree with
        upstream's in the direction that still crashes."""
        text = "\n".join(
            [_atom(1, "CA", "ALA", "A", 10),
             _atom(2, "CA", "MSE", "A", 11, record="HETATM"),
             _atom(3, "CA", "HOH", "A", 12, record="HETATM"),
             _atom(4, "CA", "VAL", "A", 13)]) + "\nEND\n"
        _p, residues = self._parsed(tmp_path, text)
        segments = rp.parse_target_input("A10-13")
        out = rp.crop_pdb_to_contig(
            text, rp.selected_residue_keys(residues, segments))
        assert "MSE" in out and "HOH" not in out
        assert len(rp.pdb_ca_residues(_write(tmp_path / "m.pdb", out))[0]) == 3

    def test_only_the_first_model_survives(self, tmp_path):
        """pdb_ca_residues counts model 1 only. An NMR ensemble staged whole
        would put N models' worth of CA on the left-hand side."""
        text = (
            "MODEL        1\n"
            + "\n".join(_atom(i + 1, "CA", "ALA", "A", i + 1) for i in range(40))
            + "\nENDMDL\nMODEL        2\n"
            + "\n".join(_atom(i + 1, "CA", "ALA", "A", i + 1) for i in range(40))
            + "\nENDMDL\nEND\n")
        _p, residues = self._parsed(tmp_path, text)
        assert len(residues) == 40
        out = rp.crop_pdb_to_contig(
            text, rp.selected_residue_keys(residues, rp.parse_target_input("A1-40")))
        assert out.count("\nATOM  ") + out.startswith("ATOM  ") == 40
        assert "MODEL" not in out and "ENDMDL" not in out

    def test_insertion_coded_twins_both_survive(self, tmp_path):
        """A13 and A13A are two residues with two CA atoms; upstream counts both
        on the left AND on the right, so the crop must keep both or the counts
        part company."""
        text = "\n".join(
            [_atom(1, "CA", "ALA", "A", 12),
             _atom(2, "CA", "VAL", "A", 13),
             _atom(3, "CA", "LEU", "A", 13, icode="A"),
             _atom(4, "CA", "GLY", "A", 14)]) + "\nEND\n"
        _p, residues = self._parsed(tmp_path, text)
        segments = rp.parse_target_input("A12-13")
        keep = rp.selected_residue_keys(residues, segments)
        assert keep == {("A", 12, ""), ("A", 13, ""), ("A", 13, "A")}
        out = rp.crop_pdb_to_contig(text, keep)
        staged, _ = rp.pdb_ca_residues(_write(tmp_path / "i.pdb", out))
        assert len(staged) == 3
        assert self._upstream_counts(staged, "A12-13") == (3, 3)

    def test_an_insertion_coded_residue_with_no_plain_sibling_survives(self, tmp_path):
        """Why the keep key carries the insertion code at all.

        The RANGE filter cannot need it: every icode variant of a residue number
        is in range exactly when the bare number is. It is needed because the
        keep key must be the PARSER's residue key. A13A here has no A13 beside
        it, so a crop keyed on ``(chain, resseq)`` alone finds nothing to match
        it against and drops a residue the contig selected — one CA short of
        upstream's count, on a paid GPU. (A survived mutation put this test
        here: keying on ``(chain, resseq, "")`` passed the twin test above.)
        """
        text = "\n".join(
            [_atom(1, "CA", "ALA", "A", 12),
             _atom(2, "CA", "LEU", "A", 13, icode="A"),
             _atom(3, "CA", "GLY", "A", 14)]) + "\nEND\n"
        _p, residues = self._parsed(tmp_path, text)
        assert ("A", 13, "") not in residues and ("A", 13, "A") in residues
        segments = rp.parse_target_input("A12-13")
        keep = rp.selected_residue_keys(residues, segments)
        assert keep == {("A", 12, ""), ("A", 13, "A")}
        out = rp.crop_pdb_to_contig(text, keep)
        staged, _ = rp.pdb_ca_residues(_write(tmp_path / "j.pdb", out))
        assert self._upstream_counts(staged, "A12-13") == (2, 2)
        assert "LEU" in out, "the insertion-coded residue must survive the crop"

    def test_cropping_is_idempotent(self, tmp_path):
        _p, residues = self._parsed(tmp_path)
        segments = rp.parse_target_input(self._CONTIG)
        once = rp.crop_pdb_to_contig(
            (tmp_path / "in.pdb").read_text(),
            rp.selected_residue_keys(residues, segments))
        again_res, _ = rp.pdb_ca_residues(_write(tmp_path / "o.pdb", once))
        assert rp.crop_pdb_to_contig(
            once, rp.selected_residue_keys(again_res, segments)) == once

    # ---- end to end, through main() ------------------------------------

    def test_the_registered_file_is_the_cropped_one(self, tmp_path, monkeypatch):
        """The wiring: --target-path must name a file that satisfies the
        assertion, and --target-input must still be the contig the user asked
        for (omitting it makes upstream default to "A1-100")."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._CONTIG, target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == self._CONTIG
        staged = Path(add[add.index("--target-path") + 1])
        residues, _ = rp.pdb_ca_residues(staged)
        left, right = self._upstream_counts(residues, self._CONTIG)
        assert left == right == 130, (
            f"the registered file would fail upstream's evaluate assertion "
            f"({left} != {right})")

    def test_the_archived_input_is_what_was_designed_against(self, tmp_path, monkeypatch):
        """run_dir/_hub_input/target.pdb is documented as the exact bytes that
        were designed against. After the crop that is the cropped file."""
        calls: list = []
        self._drive(rp, tmp_path, monkeypatch,
                    self._spec(target_input=self._CONTIG, target_chain="A B"),
                    calls=calls, pdb_text=_make_3s7g_like())
        archived = tmp_path / "proteina" / "inference" / "_hub_input" / "target.pdb"
        assert archived.is_file()
        residues, _ = rp.pdb_ca_residues(archived)
        assert self._upstream_counts(residues, self._CONTIG) == (130, 130)

    def test_a_whole_chain_contig_still_registers(self, tmp_path, monkeypatch):
        """The case that already worked must keep working — and the two chains
        the contig does not name are gone from the registered file."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="A236-443,B236-442", target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        residues, _ = rp.pdb_ca_residues(Path(add[add.index("--target-path") + 1]))
        assert sorted({r[0] for r in residues}) == ["A", "B"]
        assert self._upstream_counts(residues, "A236-443,B236-442") == (415, 415)

    def test_a_hotspot_outside_the_contig_is_refused_before_any_subprocess(
            self, tmp_path, monkeypatch):
        """UNCHANGED BEHAVIOUR, PINNED. ``missing_hotspots`` has always been
        evaluated against the contig's SELECTION rather than the whole file, so
        this already failed pre-GPU before the crop; the crop makes it true of
        the staged bytes as well. What is new is that the message distinguishes
        "that residue does not exist" from "that residue is outside the range
        you asked for", which have different fixes."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._CONTIG, target_chain="A B",
                       hotspot_spec=["A350"]),
            calls=calls, hotspot_spec=["A350"], pdb_text=_make_3s7g_like())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "hotspot_missing"
        assert calls == [], "no subprocess may run once a hotspot is unmatched"
        detail = data["error"]["detail"]
        assert "A350" in detail and self._CONTIG in detail
        assert "outside" in detail and "widen" in detail

    def test_a_hotspot_absent_from_the_file_says_so_differently(self, tmp_path):
        """The other half of the pair: A9999 is in no chain at all, so the
        widen-the-range advice would be wrong and must not appear."""
        _p, residues = self._parsed(tmp_path)
        segments = rp.parse_target_input(self._CONTIG)
        selected = rp.select_residues(residues, segments)
        assert rp.hotspots_outside_contig(residues, selected, ["A9999"]) == []
        assert rp.hotspots_outside_contig(residues, selected, ["A350"]) == ["A350"]
        # A chain the contig does not name counts as outside too — after the
        # crop chain C is not in the file either.
        assert rp.hotspots_outside_contig(residues, selected, ["C5"]) == ["C5"]

    def test_the_self_check_refuses_rather_than_paying_for_the_crash(
            self, tmp_path, monkeypatch):
        """The crop is verified on the file that will actually be registered.
        A crop that silently kept the whole upload must fail HERE, for free."""
        calls: list = []
        monkeypatch.setattr(rp, "crop_pdb_to_contig", lambda text, keep: text)
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._CONTIG, target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_crop"
        assert "415" in data["error"]["detail"] and "130" in data["error"]["detail"]
        assert calls == [], "nothing may run once the staged file is wrong"


class TestContigEndpointsMustBeRealResidues:
    """THE DEFECT THAT COST A SHARD, AND ITS SIBLING GUARD.

    3S7G has chain A spanning 236-443 and chain B spanning 236-**442**. A run
    was launched with ``A236-443,B236-443``. Upstream died::

        ValueError('No atoms found for selection: B/*/443')

    ~60 s of billed A100, zero designs.

    EVERY EXISTING GUARD PASSED IT, and each for a defensible reason rather than
    an oversight — which is why the count-based checks cannot be tightened into
    covering this and a separate question has to be asked:

    * ``select_residues`` filters ``lo <= resseq <= hi``, so chain B's segment
      picked out the 207 residues that DO exist. The count is correct.
    * step 4 ("every segment must select something") therefore saw 207, not 0.
    * the 20-residue floor passed, the hotspots passed.
    * ``unrenderable_segments`` passed — no negative numbers.
    * ``stage_cropped_target``'s self-check passed at (415, 415): it compares
      the staged file against the same selection, and both sides ignore the
      residue that is not there identically.

    Nothing asked whether ``lo`` and ``hi`` are themselves residues of that
    chain. ``AtomSelectionStack.from_contig`` does, on a paid GPU.
    """

    # Borrowed rather than inherited, for the reason stated on the class above.
    _drive = TestCustomTargetRegistration._drive
    _spec = TestCustomTargetRegistration._spec

    # The real spans, so the poison contig differs from the good one by ONE.
    _POISON = "A236-443,B236-443"
    _GOOD = "A236-443,B236-442"

    @staticmethod
    def _residues(tmp_path, text=None, spans=None):
        p = tmp_path / "in.pdb"
        p.write_text(text if text is not None
                     else _make_pdb(spans or {"A": (236, 443), "B": (236, 442)}))
        return rp.pdb_ca_residues(p)[0]

    # ---- the predicate --------------------------------------------------

    def test_the_real_failure_is_flagged_and_names_chain_b(self, tmp_path):
        """The one that cost the shard: A is fine, B is over by one."""
        residues = self._residues(tmp_path)
        assert rp.missing_endpoints(
            residues, rp.parse_target_input(self._POISON)) == [("B", 443)]

    def test_the_corrected_contig_is_accepted(self, tmp_path):
        """The guard is worthless if it also refuses the fix it recommends."""
        residues = self._residues(tmp_path)
        assert rp.missing_endpoints(
            residues, rp.parse_target_input(self._GOOD)) == []

    def test_the_premise_every_cheaper_check_passes_the_poison_contig(
            self, tmp_path):
        """SO THIS SUITE CANNOT PASS VACUOUSLY. If any of these ever starts
        failing on its own, the new guard is no longer the thing catching it and
        this class is measuring something else."""
        residues = self._residues(tmp_path)
        segments = rp.parse_target_input(self._POISON)
        # step 4: every segment selects something
        assert [len(rp.select_residues(residues, [s])) for s in segments] == [208, 207]
        # the 20-residue floor
        assert len(rp.select_residues(residues, segments)) == 415
        # step 3b: nothing negative
        assert rp.unrenderable_segments(segments) == []
        # and the crop's own self-check balances
        assert rp.stage_cropped_target(
            tmp_path / "staged.pdb", (tmp_path / "in.pdb").read_text(),
            residues, segments) == (415, 415)

    def test_a_low_endpoint_is_checked_too(self, tmp_path):
        """The failure was on ``hi``; ``lo`` has the identical exposure."""
        residues = self._residues(tmp_path)
        assert rp.missing_endpoints(
            residues, rp.parse_target_input("B200-442")) == [("B", 200)]

    def test_both_endpoints_of_one_segment_are_reported(self, tmp_path):
        residues = self._residues(tmp_path)
        assert rp.missing_endpoints(
            residues, rp.parse_target_input("B200-999")) == [("B", 200), ("B", 999)]

    def test_a_later_segment_is_checked_not_just_the_first(self, tmp_path):
        """``A236-443`` is valid, so a guard that stopped at the first segment
        would wave the run through — which is the shape of the real failure."""
        residues = self._residues(tmp_path)
        segments = rp.parse_target_input(self._POISON)
        assert rp.missing_endpoints(residues, segments[:1]) == []
        assert rp.missing_endpoints(residues, segments) == [("B", 443)]

    def test_the_bound_is_membership_not_an_off_by_one(self, tmp_path):
        """442 and 443 are one apart and must land on opposite sides; and the
        test is MEMBERSHIP, so an endpoint inside a disordered gap fails too
        even though it is well within the chain's min/max."""
        residues = self._residues(tmp_path)
        assert rp.missing_endpoints(residues, [("B", 236, 442)]) == []
        assert rp.missing_endpoints(residues, [("B", 236, 443)]) == [("B", 443)]
        gapped = [r for r in residues if not (301 <= r[1] <= 349 and r[0] == "A")]
        assert rp.missing_endpoints(gapped, [("A", 320, 443)]) == [("A", 320)]

    def test_a_bare_chain_id_has_no_endpoints_to_check(self, tmp_path):
        """``(chain, None, None)`` is legal input and must not raise or refuse.
        Skipped inside the helper, so a caller that forgets to filter gets the
        right answer rather than a TypeError."""
        residues = self._residues(tmp_path)
        assert rp.missing_endpoints(residues, [("A", None, None)]) == []
        assert rp.missing_endpoints(
            residues, [("A", None, None), ("B", 236, 443)]) == [("B", 443)]

    def test_the_derived_path_is_safe_by_construction(self, tmp_path):
        """``derive_segments`` builds spans from min/max of residues it just
        read, so both ends exist by definition. Pinned so a future change to
        derivation cannot quietly start producing endpoints that do not."""
        residues = self._residues(tmp_path)
        derived = rp.derive_segments(residues, ["A", "B"])
        assert derived == [("A", 236, 443), ("B", 236, 442)]
        assert rp.missing_endpoints(residues, derived) == []

    # ---- insertion codes -------------------------------------------------

    def test_an_endpoint_matching_any_insertion_code_counts_as_existing(
            self, tmp_path):
        """THE DELIBERATE CHOICE, PINNED. A contig endpoint is a bare number
        with nowhere to put an insertion code, so existence is tested on the
        residue number ALONE.

        The load-bearing case is chain B: residue 200 exists ONLY as ``B200A``.
        ``select_residues`` already selects it and the crop already keeps it —
        both filter on ``lo <= resseq <= hi`` with the code ignored — so
        refusing the endpoint that the selection then honours would make this
        guard disagree with the code it guards. Upstream agrees: its own
        failure names ``B/*/443``, a wildcard in the insertion-code field.
        """
        text = "\n".join([
            _atom(1, "CA", "ALA", "A", 100),
            _atom(2, "CA", "ALA", "A", 100, icode="A"),
            _atom(3, "CA", "ALA", "A", 101),
            _atom(4, "CA", "ALA", "B", 200, icode="A"),
            _atom(5, "CA", "ALA", "B", 201),
        ]) + "\nEND\n"
        residues = self._residues(tmp_path, text=text)
        assert residues == [("A", 100, ""), ("A", 100, "A"), ("A", 101, ""),
                            ("B", 200, "A"), ("B", 201, "")], "fixture check"
        # A100 exists both bare and coded; B200 exists ONLY coded. Neither is
        # missing.
        assert rp.missing_endpoints(residues, [("A", 100, 101)]) == []
        assert rp.missing_endpoints(residues, [("B", 200, 201)]) == []
        # ...and the selection really does honour the coded-only endpoint, which
        # is what makes accepting it the consistent answer rather than a lax one.
        assert rp.select_residues(residues, [("B", 200, 201)]) == [
            ("B", 200), ("B", 201)]
        # The number still has to be present under SOME code.
        assert rp.missing_endpoints(residues, [("B", 199, 201)]) == [("B", 199)]

    # ---- residue 0 (#19) -------------------------------------------------

    def test_residue_zero_is_refused_by_the_same_test_no_special_case(
            self, tmp_path):
        """ISSUE #19, CLOSED HERE. The adapter refuses ``lo < 0`` and 0 is not
        < 0, so ``A0-100`` has always been accepted. On a chain numbered from 1
        it selects residues 1-100 — non-empty and above the floor, so every
        other guard passes — while upstream is recorded as resolving residue 0
        by taking the WHOLE chain, silently designing against a different target
        than the operator asked for.

        Residue 0 simply does not exist, so it is already a missing endpoint.
        There is no rule about zero in ``missing_endpoints`` and there must not
        be one: the general test is what makes this free."""
        residues = self._residues(tmp_path, spans={"A": (1, 115)})
        segments = rp.parse_target_input("A0-100")
        # The premise: everything cheaper waves it through.
        assert px._parse_target_input("A0-100")[3] is None, (
            "the adapter still accepts A0-100 — if this ever fails, #19 moved")
        assert len(rp.select_residues(residues, segments)) == 100
        assert rp.unrenderable_segments(segments) == []
        # The guard.
        assert rp.missing_endpoints(residues, segments) == [("A", 0)]

    def test_a_chain_that_really_is_numbered_from_zero_is_not_refused(
            self, tmp_path):
        """The guard must key on the residue's absence, not on the number 0 —
        ``A0-240`` is exactly what the negative-numbering refusal recommends,
        and that advice has to keep working."""
        residues = self._residues(tmp_path, spans={"A": (0, 240)})
        assert rp.missing_endpoints(
            residues, rp.parse_target_input("A0-240")) == []

    # ---- end to end, through main() --------------------------------------

    def test_the_poison_contig_is_refused_before_any_subprocess(
            self, tmp_path, monkeypatch):
        """THE MONEY ASSERTION. ``run_streaming`` is what spawns both the
        registration and the design, and it must not be reached at all."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._POISON, target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input_endpoint"
        assert calls == [], (
            "no subprocess may run once a range endpoint names no residue")

    def test_the_refusal_names_the_chain_the_endpoint_and_the_real_span(
            self, tmp_path, monkeypatch):
        """The operator's next action is to retype the contig, so the message
        has to carry the number. ``B236-442`` must be obvious from it."""
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._POISON, target_chain="A B"),
            calls=[], pdb_text=_make_3s7g_like())
        detail = data["error"]["detail"]
        assert "chain B" in detail and "443" in detail
        assert "A236-443, B236-442" in detail, "the real spans of the upload"
        assert "B236-442" in detail, "the corrected contig, spelled out"
        # It must not misattribute the fault to chain A, which is correct.
        assert "chain A" not in detail
        # And it should say where this would otherwise have been discovered.
        assert "B/*/443" in detail

    def test_the_suggested_range_actually_works(self, tmp_path, monkeypatch):
        """Companion to the test above, in the shape the negative-numbering
        guard already uses: the contig the refusal recommends must register and
        design cleanly on the same structure."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._GOOD, target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == self._GOOD

    def test_residue_zero_is_refused_end_to_end(self, tmp_path, monkeypatch):
        """#19 through ``main()``, on a chain where ``A0-100`` selects 100
        residues so nothing cheaper can be the thing that refuses it."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="A0-100", target_chain="A"),
            calls=calls, pdb_spans={"A": (1, 115)})
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input_endpoint"
        assert calls == [], "A0-100 reached the GPU before this guard"
        detail = data["error"]["detail"]
        assert "residue 0 on chain A" in detail
        assert "A1-100" in detail, "the corrected contig"

    def test_a_low_endpoint_is_refused_end_to_end(self, tmp_path, monkeypatch):
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="A236-443,B200-442", target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input_endpoint"
        assert calls == []
        assert "residue 200 on chain B" in data["error"]["detail"]
        assert "B236-442" in data["error"]["detail"]

    def test_a_bare_chain_id_still_registers(self, tmp_path, monkeypatch):
        """Unaffected path 1: the whole-chain form resolves through
        min/max in step 3 and must not be refused."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="A,B", target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == "A236-443,B236-442"

    def test_a_derived_no_contig_run_still_registers(self, tmp_path, monkeypatch):
        """Unaffected path 2: no ``target_input`` at all, so the contig comes
        from ``derive_segments`` and both ends exist by construction."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="", target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == "A236-443,B236-442"

    def test_an_empty_range_still_reports_the_older_clearer_message(
            self, tmp_path, monkeypatch):
        """ORDERING, PINNED. A range that selects nothing at all keeps step 4's
        message; the new guard is the narrower one for a range that selects
        REAL residues either side of an end that does not exist. Both would fire
        on ``Z1-99``, and step 4 says the more useful thing."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="Z1-99", target_chain="A"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input"
        assert calls == []


class TestNumericChainsAndUnboundedRangesAreAlreadyRefused:
    """BACKLOG #21, VERIFIED BY EXECUTION RATHER THAN BY READING.

    The item says "numeric chain ids and unbounded hi reach the GPU unchecked".
    They do not, at either layer, and these tests exist so the item can be
    closed against something that runs:

    * the adapter's ``_SEGMENT_RE`` is ``^([A-Za-z])(-?\\d+)-(-?\\d+)$`` — a
      numeric chain fails ``[A-Za-z]`` and a missing ``hi`` fails ``\\d+`` — and
      a non-match returns an actionable error before a campaign exists;
    * the container's ``parse_target_input`` re-checks independently, because it
      must never trust a value it did not check itself: ``chain.isalpha()``
      rejects the first and ``int('')`` the second, and ``prepare_custom_target``
      turns the ValueError into a ``_fail`` before anything is spawned.

    Pinned at both layers so the two cannot silently diverge and so the closure
    stays true.
    """

    NUMERIC = ["1236-443", "1", "12-30", "A1-60,2-30", "1A-60"]
    UNBOUNDED = ["A236-", "A236", "A-", "A236-443-", "A1-60,B12-"]

    @pytest.mark.parametrize("raw", NUMERIC + UNBOUNDED)
    def test_the_adapter_refuses_it(self, raw):
        segs, canon, _chains, err = px._parse_target_input(raw)
        assert err is not None, f"{raw!r} passed the adapter"
        assert segs == [] and canon == ""
        assert "not valid" in err and "A1-150" in err, "the error must be usable"

    @pytest.mark.parametrize("raw", NUMERIC + UNBOUNDED)
    def test_the_container_refuses_it_independently(self, raw):
        with pytest.raises(ValueError, match="unparsable target_input segment"):
            rp.parse_target_input(raw)

    def test_a_numeric_chain_never_reaches_a_subprocess(self, tmp_path, monkeypatch):
        """Through ``main()``, since the adapter is not the only entry point —
        the container is what a replayed or hand-built payload hits."""
        calls: list = []
        data = TestCustomTargetRegistration._drive(
            self, rp, tmp_path, monkeypatch,
            TestCustomTargetRegistration._spec(self, target_input="1236-443"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input"
        assert calls == []

    def test_an_unbounded_hi_never_reaches_a_subprocess(self, tmp_path, monkeypatch):
        calls: list = []
        data = TestCustomTargetRegistration._drive(
            self, rp, tmp_path, monkeypatch,
            TestCustomTargetRegistration._spec(self, target_input="A236-"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input"
        assert calls == []


# ---------------------------------------------------------------------------
# 8. Templates parse
# ---------------------------------------------------------------------------


class TestNegativeResidueNumbering:
    """A contig upstream cannot parse must be refused before the GPU boots.

    Verified against atomworks ``AtomSelectionStack.from_contig``
    (``src/atomworks/io/utils/selection.py``)::

        CONTIG_REGEX = re.compile(r"([A-Za-z]+)(\\d+)-(\\d+)")
        match = CONTIG_REGEX.match(selection)
        if not match:
            raise ValueError(f"Invalid contig string: {selection}")

    ``(\\d+)`` is unsigned. A structure that keeps its expression tag is
    author-numbered from a negative index, so the derived contig reads
    ``A-5-240`` and raises. Every other pre-GPU guard passes on such a target:
    the residues exist, the selection is non-empty, the registry write and the
    read-back both succeed. Without the guard the refusal lands mid-``complexa
    design`` on a billed A100.
    """

    # Upstream's regex, transcribed. If upstream ever widens it, this test
    # starts over-refusing and the mismatch is visible here first.
    UPSTREAM_CONTIG_RE = r"([A-Za-z]+)(\d+)-(\d+)"

    def test_negative_bounds_are_flagged(self):
        assert rp.unrenderable_segments([("A", -5, 240)]) == [("A", -5, 240)]
        assert rp.unrenderable_segments([("A", -20, -5)]) == [("A", -20, -5)]

    def test_zero_and_positive_bounds_are_fine(self):
        """0 is legal — "A0-240" matches upstream's regex. The bound is < 0,
        not <= 0, and narrowing it further would refuse valid targets."""
        assert rp.unrenderable_segments([("A", 0, 240)]) == []
        assert rp.unrenderable_segments([("A", 1, 240)]) == []
        assert rp.unrenderable_segments([("A", 1, 60), ("B", 1, 40)]) == []

    def test_only_the_offending_segment_is_returned(self):
        segs = [("A", 1, 60), ("B", -3, 40), ("C", 2, 30)]
        assert rp.unrenderable_segments(segs) == [("B", -3, 40)]

    def test_every_contig_we_emit_round_trips_through_upstreams_regex(self):
        """The invariant the guard exists to hold: anything format_contig()
        renders for segments we accept must parse upstream."""
        import re
        accepted = [("A", 0, 240), ("A", 1, 115), ("B", 12, 157), ("C", 73, 99)]
        assert rp.unrenderable_segments(accepted) == []
        for token in rp.format_contig(accepted).split(","):
            assert re.match(self.UPSTREAM_CONTIG_RE, token), token

    def test_upstream_regex_really_rejects_what_we_refuse(self):
        """Pins the premise. If this ever passes, the guard is over-refusing."""
        import re
        rendered = rp.format_contig([("A", -5, 240)])
        assert rendered == "A-5-240"
        assert re.match(self.UPSTREAM_CONTIG_RE, rendered) is None

    def test_adapter_refuses_a_typed_negative_range(self):
        segs, canon, chains, err = px._parse_target_input("A-5-240")
        assert segs == [] and canon == ""
        assert err is not None and "negative residue numbers" in err

    def test_adapter_accepts_a_range_starting_at_zero(self):
        segs, canon, chains, err = px._parse_target_input("A0-240")
        assert err is None
        assert segs == [("A", 0, 240)] and canon == "A0-240"

    def test_adapter_refuses_a_negative_segment_among_valid_ones(self):
        _segs, _canon, _chains, err = px._parse_target_input("A1-60,B-3-40")
        assert err is not None and "negative residue numbers" in err


class TestTemplatesParse:
    def _templates(self):
        return Path(__file__).resolve().parents[1] / "templates"

    def test_results_template_parses(self):
        from jinja2 import Environment
        src = (self._templates() / "tools" / "proteina_results.html").read_text(encoding="utf-8")
        Environment().parse(src)

    def test_form_template_parses(self):
        from jinja2 import Environment
        src = (self._templates() / "tools" / "proteina_form.html").read_text(encoding="utf-8")
        Environment().parse(src)

    def test_launch_page_preset_control_keeps_its_id(self):
        """launch.html's estimate JS resolves a variant tool's preset with
        getElementById(tool + '__preset') and falls back to 'pilot' when that
        returns null. proteina has no 'pilot' preset, so the estimate endpoint
        rejects the request, the Launch button never enables, and every tool
        co-selected with proteina is blocked too (one verdict per request)."""
        src = (self._templates() / "targets" / "launch.html").read_text(encoding="utf-8")
        assert 'id="proteina__preset"' in src

    def test_launch_copy_no_longer_denies_hotspots(self):
        """The launch screen asserted "Proteina takes no hotspots and no binder
        length" for as long as that was true. It is not any more, and stale copy
        that contradicts the form is how a user concludes the field is ignored."""
        src = (self._templates() / "targets" / "launch.html").read_text(encoding="utf-8")
        assert "takes no hotspots" not in src



# ---------------------------------------------------------------------------
# 9. Image reproducibility: the Dockerfile guards and the local entrypoints
# ---------------------------------------------------------------------------
#
# WHY A TEST READS A DOCKERFILE AS TEXT. On 2026-08-04 a fresh build of
# ``tools/proteina/Dockerfile.modal`` produced an image in which EVERY design
# run died at import — before any target, hotspot or config was read — while
# the image built from the same file on 2026-07-16 and still deployed kept
# working. Cause: ``env/build_uv_env.sh`` pulls dm-haiku transitively with no
# version bound, dm-haiku 0.0.17 (released 2026-07-27) hoisted
# ``jax.core.take_current_trace`` to module scope, and the pinned jax 0.4.29 has
# no such attribute. The two guards added in response — the ``dm-haiku==0.0.16``
# pin and the build-time import gate — are only worth anything while they are
# still IN the file, and nothing else offline can notice their removal. A real
# build is the only thing that proves they WORK; these tests prove they are
# still there and still in the right order, which is the part a tidy-up can
# take away.

_PROTEINA_DIR = Path(__file__).resolve().parents[1] / "tools" / "proteina"
_DOCKERFILE_PATH = _PROTEINA_DIR / "Dockerfile.modal"

# The version the last known-good image actually carries (read out of deployed
# image im-r2dPPY4ITG1kCGGRwKqDfS, built 2026-07-16, alongside jax==0.4.29 and
# jaxlib==0.4.29+cuda12.cudnn91), not the newest version that happens to work.
_GOOD_DM_HAIKU = "dm-haiku==0.0.16"


def _dockerfile_instructions():
    """``[(lineno, text)]`` — one entry per Dockerfile instruction, with
    backslash continuations joined and comment lines dropped.

    ``lineno`` is the line the instruction STARTS on, which is what the
    ordering assertions compare. Docker itself joins continuations, so a guard
    split across two lines must not read as absent.
    """
    out = []
    current = None
    for lineno, raw in enumerate(
            _DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if current is None:
            if not stripped:
                continue
            current = [lineno, stripped]
        else:
            current[1] += " " + stripped
        if current[1].endswith("\\"):
            current[1] = current[1][:-1].rstrip()
        else:
            out.append((current[0], current[1]))
            current = None
    if current is not None:
        out.append((current[0], current[1]))
    return out


def _instructions_matching(*needles):
    return [(lineno, text) for lineno, text in _dockerfile_instructions()
            if all(needle in text for needle in needles)]


def _dm_haiku_installs():
    """Every instruction that installs dm-haiku, matched on the PACKAGE rather
    than on the installer.

    Deliberately not keyed to "uv pip install" or "python -m pip": the
    installer here has already been wrong once (``/opt/proteina/.venv/bin/uv``
    was referenced for months and never existed), and a test that pins the
    spelling of the installer would have to be rewritten every time that is
    corrected — which is exactly when it most needs to still be checking.
    """
    return _instructions_matching("dm-haiku")


def _run_validate_imported_modules():
    """The module names ``run_pipeline.run_validate`` passes to
    ``importlib.import_module``, read from the source rather than assumed.

    The build gate exists to be the same test as the free validate tier. If
    that tier grows a third import and the gate does not, the gate stops
    standing in for the thing it gates — so this is derived, not hardcoded.
    """
    src = (_PROTEINA_DIR / "run_pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(_PROTEINA_DIR / "run_pipeline.py"))
    fn = next((node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == "run_validate"),
              None)
    assert fn is not None, (
        "run_pipeline.run_validate has been renamed or removed; the build-time "
        "import gate in Dockerfile.modal was written to mirror it, so decide "
        "what the gate should mirror now instead of deleting this test")
    modules = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            modules.add(node.args[0].value)
    return modules


def _run_pipeline_module_scope_third_party():
    """Third-party packages ``run_pipeline.py`` imports at MODULE scope.

    Module scope is what makes these fatal for EVERY tier: they are resolved
    when the interpreter loads the file, before the payload is parsed and
    before any tier branches, so a missing one kills the FREE validate tier as
    surely as a paid design run — at import, inside a billing container, having
    produced nothing.

    Deliberately module-scope only (``tree.body``, not ``ast.walk``): rdkit and
    PyYAML are imported lazily inside functions that catch the ImportError and
    fail cleanly, so they are a different, cheaper failure class and do not
    belong in a build gate. If one of them ever moves up to module scope this
    starts requiring it, which is the intent.
    """
    path = _PROTEINA_DIR / "run_pipeline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    return {name for name in roots
            if name != "__future__" and name not in sys.stdlib_module_names}


def _modal_app_subprocess_interpreter():
    """The interpreter ``modal_app.run_tool`` actually spawns run_pipeline.py
    with, read from the source rather than assumed.

    This is the interpreter PRODUCTION uses, so it is the one the build gate
    has to prove works. It is a bare name resolved through ``PATH``, which is
    the whole reason the gate cannot be satisfied by an absolute path alone.
    """
    path = _PROTEINA_DIR / "modal_app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
            continue
        elts = node.value.elts
        if not any(isinstance(e, ast.Name) and e.id == "_RUN_PIPELINE_REMOTE"
                   for e in elts):
            continue
        if elts and isinstance(elts[0], ast.Constant) and isinstance(elts[0].value, str):
            return elts[0].value
    return None


def _shell_skeleton(text):
    """``text`` with every quoted span blanked out, leaving only shell syntax.

    The import gate's payload is ``python -c "import importlib; ..."`` — full
    of semicolons that are PYTHON, not shell. Scanning the raw instruction for
    shell control flow would flag those, so quoted spans become spaces and only
    the skeleton is examined. No instruction in this file uses a
    backslash-escaped quote; if one ever does, teach this function about it
    rather than dropping the check.
    """
    out = []
    quote = None
    for ch in text:
        if quote is not None:
            out.append(" ")
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


# Shell constructs that turn a failing command into a passing one, or send its
# output somewhere nobody will read. ``&&`` is the one operator that PROPAGATES
# failure, so it is removed before the scan instead of being listed here.
# Order matters: the longer spellings are stripped first so ``||`` is not also
# reported as ``|``, and ``>>`` not as ``>``.
_FAILURE_SWALLOWING_OPERATORS = ("||", ">>", "2>&1", ";", "|", "&", ">", "<")


def _failure_swallowing_operators(text):
    """Which of the above appear in ``text``'s shell skeleton.

    THE CHEAPEST WAY TO LOSE BOTH GUARDS AT ONCE. Every text assertion in this
    file passes on ``RUN <pin> || true`` and on ``RUN <gate> > /dev/null 2>&1
    || echo 'gate skipped'``: the pin is still spelled ``==0.0.16`` with
    ``--no-deps``, the gate still names both modules — and neither can fail any
    more. A build that dies on one of these lines has found exactly the class
    of breakage they exist to stop, so the only correct response is to fix the
    cause, never to silence the check.
    """
    skeleton = _shell_skeleton(text).replace("&&", "  ")
    found = []
    for token in _FAILURE_SWALLOWING_OPERATORS:
        if token in skeleton:
            found.append(token)
            skeleton = skeleton.replace(token, " " * len(token))
    return found


def _runtime_dep_installs():
    """Instructions installing run_pipeline.py's own runtime deps.

    Matched on the PACKAGE, like ``_dm_haiku_installs`` and for the same
    reason: the installer spelling on this line has already been wrong once.
    """
    return _instructions_matching("pip install", "requests")


# Every Python-package installer this image uses. ``apt-get install`` is
# deliberately absent: system libraries cannot resolve a package inside
# /opt/proteina/.venv, which is the invariant being protected.
_PACKAGE_INSTALL_NEEDLES = (
    "pip install", "pip3 install", "pip uninstall", "conda install",
    "poetry add", "uv add", "uv sync", "build_uv_env.sh",
    "setup.py install", "easy_install",
)


def _package_installing_instructions():
    return [(lineno, text) for lineno, text in _dockerfile_instructions()
            if any(needle in text for needle in _PACKAGE_INSTALL_NEEDLES)]


_VENV_BIN = "/opt/proteina/.venv/bin"

# ``PATH=`` at a token boundary. PYTHONPATH, DATA_PATH, CKPT_PATH, CKPT_DIR's
# neighbours and RF3_CKPT_PATH all end in the same five characters and are not
# the shell's search path.
_PATH_ASSIGNMENT_RE = re.compile(r"(?<![A-Z0-9_])PATH=")


def _path_env_assignments():
    """``ENV`` instructions that assign the shell ``PATH``, in file order.

    The LAST one wins in the built image, which is what the ordering assertion
    below keys on.
    """
    return [(lineno, text) for lineno, text in _dockerfile_instructions()
            if text.startswith("ENV ") and _PATH_ASSIGNMENT_RE.search(text)]


class TestDockerfileDmHaikuPin:
    """The pin that fixes the drift that happened."""

    def test_dm_haiku_is_pinned_to_the_version_the_good_image_carries(self):
        installs = _dm_haiku_installs()
        assert installs, (
            f"Dockerfile.modal no longer pins {_GOOD_DM_HAIKU}. Without it "
            "build_uv_env.sh resolves dm-haiku unbounded (colabdesign 1.1.1 / "
            "alphafold-colabfold 2.3.7 pull it transitively, and dm-haiku "
            "declares jax only as an optional extra), and 0.0.17+ evaluates "
            "jax.core.take_current_trace at module scope, which jax 0.4.29 does "
            "not have. Every design run then dies at import on a billing GPU.")
        for lineno, text in installs:
            assert _GOOD_DM_HAIKU in text, (
                f"Dockerfile.modal line {lineno} installs dm-haiku at a version "
                f"other than the one an A/B/A swap in the known-good image "
                f"proved sufficient ({_GOOD_DM_HAIKU}): {text!r}")

    def test_the_pin_uses_no_deps(self):
        """``--no-deps`` is load-bearing, not decoration.

        0.0.16's requirements (absl-py, jmp, numpy, tabulate) are already
        satisfied in that venv. A resolving install running AFTER the CUDA
        wheel steps can replace ``jaxlib==0.4.29+cuda12.cudnn91`` with a
        CPU-only jaxlib — which does not fail the build, it silently moves AF2
        onto CPU and turns every design run into a 7200 s timeout at full GPU
        price.
        """
        installs = _dm_haiku_installs()
        assert installs, "no dm-haiku pin at all — see the test above"
        for lineno, text in installs:
            assert "--no-deps" in text, (
                f"Dockerfile.modal line {lineno} installs {_GOOD_DM_HAIKU} "
                "WITHOUT --no-deps; a resolving install here can swap the "
                f"CUDA jaxlib for a CPU-only one: {text!r}")

    def test_the_pin_installs_into_the_projects_venv(self):
        """Into ``/opt/proteina/.venv``, which is where the reward stack lives.

        A bare ``pip install`` would put 0.0.16 in the base image's system
        Python, leave the venv on whatever floated in, and pass every other
        assertion here.
        """
        for lineno, text in _dm_haiku_installs():
            assert "/opt/proteina/.venv/bin/" in text, (
                f"Dockerfile.modal line {lineno} does not install the pin into "
                f"/opt/proteina/.venv: {text!r}")

    def test_the_pin_runs_after_the_upstream_env_build(self):
        """Order decides the outcome: build_uv_env.sh installs the floating
        dm-haiku, so a pin placed before it is simply overwritten."""
        builds = _instructions_matching("build_uv_env.sh")
        installs = _dm_haiku_installs()
        assert builds and installs
        assert min(l for l, _ in installs) > max(l for l, _ in builds), (
            "the dm-haiku pin is applied BEFORE env/build_uv_env.sh runs, so "
            "the build script's own unbounded resolution replaces it")

    def test_the_pin_runs_after_pip_exists_in_the_venv(self):
        """A uv-created venv ships NO pip; ``ensurepip`` is what puts one there.

        Move the pin above that step and the build fails outright — which is
        survivable — but the tempting "fix" is to reach for the venv's uv, and
        there isn't one (see the test below).
        """
        ensurepip = _instructions_matching("ensurepip")
        installs = [(lineno, text) for lineno, text in _dm_haiku_installs()
                    if "-m pip" in text]
        if not installs:
            pytest.skip("the pin no longer uses the venv's pip")
        assert ensurepip, (
            "the pin uses `python -m pip` but nothing bootstraps pip into the "
            "uv-created venv any more")
        assert min(l for l, _ in installs) > max(l for l, _ in ensurepip), (
            "the dm-haiku pin runs before ensurepip, so the venv has no pip yet")

    def test_nothing_reaches_for_a_uv_that_is_not_in_the_venv(self):
        """The dead branch this pin's review uncovered.

        ``RUN /opt/proteina/.venv/bin/uv pip install requests rdkit || <pip
        fallback>`` sat here for months. The first branch could never run —
        verified inside the real image on 2026-08-04, that path does not exist;
        the curl installer puts uv at /root/.local/bin/uv — so every build was
        silently taking the fallback. A command that cannot succeed but is
        masked by ``||`` is indistinguishable from one that works.
        """
        text = _DOCKERFILE_PATH.read_text(encoding="utf-8")
        offenders = [lineno for lineno, line
                     in enumerate(text.splitlines(), 1)
                     if "/opt/proteina/.venv/bin/uv" in line
                     and not line.strip().startswith("#")]
        assert not offenders, (
            f"Dockerfile.modal lines {offenders} invoke "
            "/opt/proteina/.venv/bin/uv, which does not exist in the image; uv "
            "installs to /root/.local/bin/uv and build_uv_env.sh creates the "
            "venv WITH it, not INTO it")

    def test_the_pins_failure_cannot_be_swallowed(self):
        """``RUN ... "dm-haiku==0.0.16" || true`` passes every OTHER assertion
        in this class — the version is right, ``--no-deps`` is there, the venv
        is named, the ordering holds — while the pin no longer pins anything.

        The realistic route in is a flaky PyPI fetch failing one build and
        somebody appending ``|| true`` to get unblocked. Asserted on the PARSED
        instruction, not the raw file: the comments above this step quote the
        dead ``uv ... || <pip fallback>`` line verbatim, so a substring scan of
        the text would fire on the documentation of the bug it describes.
        """
        installs = _dm_haiku_installs()
        assert installs, "no dm-haiku pin at all — see the test above"
        for lineno, text in installs:
            swallowed = _failure_swallowing_operators(text)
            assert not swallowed, (
                f"Dockerfile.modal line {lineno} discards the exit status of "
                f"the dm-haiku pin via {swallowed}: {text!r}. A build failing "
                "here has found the drift the pin exists to stop — fix the "
                "cause, do not silence the check.")

    def test_the_pins_justification_stays_attached_to_the_pin(self):
        """The rationale block used to sit ~24 lines above the pin, directly
        over an unrelated ``download_startup.sh`` step that carried its own
        LigandMPNN comment. Read top-down it looked like a stale block someone
        had forgotten to delete — and deleting "that stale block" would have
        taken the ENTIRE justification for the pin with it, on a line whose
        only defence is that a reader understands why it is there.

        Comments and blank lines between the two are fine; an INSTRUCTION
        between them means they have come apart again.
        """
        lines = _DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines()
        pin = next((i for i, line in enumerate(lines)
                    if line.startswith("RUN ") and _GOOD_DM_HAIKU in line), None)
        assert pin is not None, "no dm-haiku pin at all — see the test above"
        header = next((i for i, line in enumerate(lines)
                       if line.lstrip().startswith("#") and "PIN dm-haiku" in line), None)
        assert header is not None and header < pin, (
            "Dockerfile.modal has a dm-haiku pin with no `PIN dm-haiku` "
            "rationale comment above it. That comment is the only thing "
            "standing between the pin and a future tidy-up; if it was reworded, "
            "reword this test with it rather than dropping the check.")
        intervening = [(i + 1, line) for i, line
                       in enumerate(lines[header:pin], start=header)
                       if line.strip() and not line.lstrip().startswith("#")]
        assert not intervening, (
            f"Dockerfile.modal separates the dm-haiku rationale (line "
            f"{header + 1}) from the pin it justifies (line {pin + 1}) with "
            f"{intervening}. Orphaned like that it reads as stale and gets "
            "deleted; keep them adjacent.")

    def test_the_pin_is_the_last_thing_that_installs_a_python_package(self):
        """The invariant the comment above the pin claims, finally asserted.

        "It must be the LAST step that can touch a Python package, so that
        nothing downstream can resolve it away again." A
        ``pip install --upgrade colabdesign`` slipped in below it re-runs the
        unbounded resolution the pin exists to correct, and every other
        assertion in this file still passes.
        """
        installers = _package_installing_instructions()
        installs = _dm_haiku_installs()
        assert installers and installs
        pin = max(lineno for lineno, _ in installs)
        latest, text = max(installers)
        assert latest == pin, (
            f"Dockerfile.modal line {latest} installs a Python package AFTER "
            f"the dm-haiku pin on line {pin}: {text!r}. Anything resolving "
            "dependencies below the pin can replace dm-haiku (or the CUDA "
            "jaxlib) again, which is the entire failure class being fixed.")


class TestDockerfileRuntimeDeps:
    """``requests`` + RDKit, on the line the dm-haiku fix rewrote.

    ``run_pipeline.py`` imports ``requests`` at MODULE scope, so it is resolved
    before the tier is even read. Two mutations survived every other test here:
    deleting this install outright, and tidying it to
    ``RUN python -m pip install --no-cache-dir requests rdkit``. The second is
    the dangerous one — the nvcr base image has its own python AND its own pip,
    so it BUILDS CLEAN, installs into the wrong interpreter, and leaves the
    venv without requests. Every tier including free validate then dies at
    interpreter start-up on a billing GPU: the dm-haiku failure class exactly,
    on a different package.
    """

    def test_the_runtime_deps_are_installed_at_all(self):
        assert _runtime_dep_installs(), (
            "Dockerfile.modal no longer installs `requests`. run_pipeline.py "
            "imports it at module scope (and modal_app.py runs that file as a "
            "subprocess), so without it EVERY tier — free validate included — "
            "raises ModuleNotFoundError at interpreter start-up inside the GPU "
            "container, after the container has been billed for startup.")

    def test_rdkit_ships_alongside_it(self):
        """RDKit is deliberately absent from the tools-hub web tier, so the
        in-container SDF -> HETATM PDB conversion has nowhere else to come
        from. It is imported lazily in run_pipeline.sdf_to_pdb, so the build
        gate does not import it — this is the only guard it has."""
        installs = _runtime_dep_installs()
        assert installs, "no runtime-dep install at all — see the test above"
        assert any("rdkit" in text for _lineno, text in installs), (
            "Dockerfile.modal installs requests but no longer installs rdkit; "
            "the ligand variant's SDF conversion has no other source of it")

    def test_the_runtime_deps_go_into_the_projects_venv(self):
        """THE MUTATION THAT BUILDS CLEAN AND FAILS ON A GPU."""
        for lineno, text in _runtime_dep_installs():
            assert f"{_VENV_BIN}/" in text, (
                f"Dockerfile.modal line {lineno} does not install requests/rdkit "
                f"into {_VENV_BIN}: {text!r}. A bare `python -m pip install` "
                "resolves to the nvcr base image's interpreter, which has its "
                "own pip — so the build succeeds, the build-time import gate "
                "still passes, and the venv that every run actually uses is "
                "left without the package.")

    def test_the_runtime_dep_installs_failure_cannot_be_swallowed(self):
        for lineno, text in _runtime_dep_installs():
            swallowed = _failure_swallowing_operators(text)
            assert not swallowed, (
                f"Dockerfile.modal line {lineno} discards the exit status of "
                f"the runtime-dep install via {swallowed}: {text!r}. This line "
                "used to carry a `|| <fallback>` whose first branch could never "
                "run, and that is precisely how it stayed broken for months.")


class TestDockerfileInterpreterPath:
    """PATH is what makes the venv the production interpreter.

    ``modal_app.py`` spawns ``["python3", "-u", "/opt/proteina/run_pipeline.py"]``
    — a BARE name. The only thing that makes it the venv's python3 is
    ``PATH=/opt/proteina/.venv/bin:$PATH`` in the ENV block. Deleting that entry
    survived the whole suite, and it silently moves every paid run onto the base
    image's Python, where proteinfoundation is not installed at all.
    """

    def test_the_venv_is_first_on_path(self):
        assignments = _path_env_assignments()
        assert assignments, (
            "Dockerfile.modal sets no PATH at all; the venv can no longer be "
            "reached by the bare `python3` modal_app.py spawns")
        lineno, text = assignments[-1]      # the last assignment wins
        assert f"PATH={_VENV_BIN}:$PATH" in text, (
            f"Dockerfile.modal line {lineno} is the last PATH assignment and it "
            f"does not put {_VENV_BIN} FIRST: {text!r}. Appending it instead "
            "(PATH=$PATH:...) leaves the base image's python3 winning the "
            "lookup, which is indistinguishable from deleting it.")

    def test_the_pipeline_is_spawned_with_a_bare_interpreter_name(self):
        """The premise of the test above, read from modal_app.py rather than
        assumed. If run_tool is ever changed to spawn an absolute path, PATH
        stops being load-bearing and these tests should be revisited, not
        silently left asserting a stale contract."""
        interpreter = _modal_app_subprocess_interpreter()
        assert interpreter, (
            "modal_app.py no longer spawns _RUN_PIPELINE_REMOTE via a list "
            "literal whose first element is a constant; re-derive which "
            "interpreter production uses before trusting the PATH assertions")
        assert "/" not in interpreter, (
            f"modal_app.py now spawns run_pipeline.py with {interpreter!r}, an "
            "absolute path rather than a PATH lookup — the ENV PATH entry and "
            "the gate's bare-interpreter half were written for a bare name")


class TestDockerfileImportGate:
    """The durable half: a broken image cannot be built, let alone deployed."""

    @staticmethod
    def _gates():
        return _instructions_matching("RUN ", "import_module",
                                      "proteinfoundation")

    def test_the_build_time_import_gate_exists(self):
        gates = self._gates()
        assert gates, (
            "Dockerfile.modal has no build-time import gate. Pinning one "
            "package stops the drift that happened; this stops the next one. "
            "Without it a resolution failure is discovered by a paying job "
            "inside a GPU container instead of by the build.")

    def test_the_gate_imports_exactly_what_run_validate_imports(self):
        """Build gate and free validate tier must test the same thing."""
        expected = _run_validate_imported_modules()
        assert expected, (
            "run_validate imports nothing via importlib any more — re-derive "
            "what the gate should mirror")
        gate_text = " ".join(text for _lineno, text in self._gates())
        assert gate_text, "no import gate — see the test above"
        missing = sorted(name for name in expected if repr(name).strip("'") not in gate_text)
        assert not missing, (
            f"run_validate imports {sorted(expected)} but the Dockerfile gate "
            f"does not cover {missing}. A drift that breaks one of those would "
            "build clean and then fail on a GPU.")

    def test_the_gate_comes_after_the_env_block(self):
        """It needs ``PYTHONPATH=/opt/proteina/src`` to find the package at all
        and ``DATA_PATH`` for the eager ``${oc.env:DATA_PATH}`` resolution. Move
        it above the ENV block and it fails for a reason that has nothing to do
        with the environment being broken."""
        env_blocks = _instructions_matching("ENV PROTEINA_HOME")
        assert env_blocks, (
            "the ENV PROTEINA_HOME block is gone; the gate's placement rule was "
            "written against it")
        gates = self._gates()
        assert gates, "no import gate — see the test above"
        assert min(l for l, _ in gates) > max(l for l, _ in env_blocks), (
            "the build-time import gate runs BEFORE the ENV block that supplies "
            "PYTHONPATH and DATA_PATH, so it cannot import the package")

    def test_the_gate_names_the_venv_interpreter_in_full(self):
        """Bare ``python`` WOULD resolve — PATH=/opt/proteina/.venv/bin:$PATH is
        set in the ENV block above it — but a path in this file has already
        lied once (the venv-local ``uv`` that was never there). A gate that
        silently tested the base image's Python would report a healthy
        environment that no design run uses, which is worse than no gate."""
        for lineno, text in self._gates():
            assert "/opt/proteina/.venv/bin/python" in text, (
                f"Dockerfile.modal line {lineno} runs the import gate on an "
                f"interpreter it does not name in full: {text!r}")

    def test_the_gate_does_not_reach_for_the_volume_mounts(self):
        """It must stay runnable with no GPU and no Volumes.

        ``/opt/proteina/{ckpts,rewards}`` are EMPTY at build time — the Volumes
        supply their contents at runtime. A gate "improved" to also check the
        weights would fail every build for a reason that is not a defect, and
        the fix for that failing build would be to delete the gate.
        """
        for lineno, text in self._gates():
            for mount in ("/opt/proteina/ckpts", "/opt/proteina/rewards"):
                assert mount not in text, (
                    f"Dockerfile.modal line {lineno} makes the import gate "
                    f"depend on {mount}, which is empty until a Volume is "
                    f"mounted at runtime: {text!r}")

    def test_the_gate_covers_run_pipelines_module_scope_third_party_imports(self):
        """Mirroring ``run_validate`` is not enough on its own.

        ``run_validate``'s two importlib calls are what the gate was written
        against, but ``run_pipeline.py`` also imports ``requests`` at MODULE
        scope — resolved before main() reads the payload, so a venv missing it
        kills the free validate tier and every paid tier alike. The gate
        imported only ``proteinfoundation``, so redirecting the requests/rdkit
        install at the base interpreter built clean, passed the gate, and
        failed on a billing GPU. Derived from the source, never hardcoded: a
        new module-scope dependency must be added to the gate with it.
        """
        expected = _run_pipeline_module_scope_third_party()
        assert expected, (
            "run_pipeline.py has no third-party module-scope imports any more; "
            "re-derive what the gate should cover instead of deleting this")
        gate_text = " ".join(text for _lineno, text in self._gates())
        assert gate_text, "no import gate — see the test above"
        missing = sorted(name for name in expected if name not in gate_text)
        assert not missing, (
            f"run_pipeline.py imports {sorted(expected)} at module scope but "
            f"the build-time import gate does not cover {missing}. A venv "
            "missing one of those builds clean, deploys clean, and then dies "
            "at interpreter start-up in EVERY container, free tier included.")

    def test_the_gate_also_runs_the_interpreter_production_runs(self):
        """The gate hardcoded ``/opt/proteina/.venv/bin/python``; production
        runs bare ``python3`` off PATH. Testing only the absolute path makes
        the gate LESS representative of the runtime, not more — it is the one
        spelling no design run ever uses. Both halves are needed: the absolute
        path proves the venv is sound, the bare name proves PATH points at it.
        """
        interpreter = _modal_app_subprocess_interpreter()
        assert interpreter, (
            "modal_app.py no longer spawns run_pipeline.py via a list literal; "
            "re-derive which interpreter the gate has to exercise")
        gate_text = " ".join(text for _lineno, text in self._gates())
        assert gate_text, "no import gate — see the test above"
        assert re.search(rf"(?<![\w./-]){re.escape(interpreter)}\s", gate_text), (
            f"the build-time import gate never invokes a bare {interpreter!r}, "
            "so it cannot notice a PATH that no longer resolves to "
            f"{_VENV_BIN}. modal_app.py spawns run_pipeline.py with exactly "
            "that bare name, so the gate would stay green while every paid run "
            "executed on the base image's Python.")

    def test_the_gate_proves_path_resolves_into_the_venv(self):
        """The bare-interpreter half has to ASSERT where it landed.

        Without an explicit check the two halves are only distinguishable when
        the base image happens to lack the package — true for
        proteinfoundation, not necessarily true for ``requests``, which base
        images very often ship.
        """
        gate_text = " ".join(text for _lineno, text in self._gates())
        assert gate_text, "no import gate — see the test above"
        assert "sys.executable" in gate_text and f"{_VENV_BIN}/" in gate_text, (
            "the import gate does not check sys.executable against "
            f"{_VENV_BIN}, so a PATH regression can only be caught by luck "
            "(the base interpreter happening not to have one of the imports)")

    def test_the_gates_failure_cannot_be_swallowed(self):
        """``RUN <gate> || true`` and ``RUN <gate> > /dev/null 2>&1 || echo
        'gate skipped'`` both satisfy every other assertion here — the gate
        still names both modules, still runs last, still names the venv — and
        neither can fail a build any more. A gate whose exit status is
        discarded is not a gate.

        Parsed instruction, not raw text: the comment block above the gate
        names these constructs in order to forbid them.
        """
        gates = self._gates()
        assert gates, "no import gate — see the test above"
        for lineno, text in gates:
            swallowed = _failure_swallowing_operators(text)
            assert not swallowed, (
                f"Dockerfile.modal line {lineno} discards the exit status or "
                f"the output of the import gate via {swallowed}: {text!r}")

    def test_the_gate_is_the_final_run_instruction(self):
        """A LAYER, NOT A POSTCONDITION. Docker runs instructions in order and
        stops caring once the gate's layer is committed, so anything appended
        below it is entirely ungated — ``pip install --upgrade jax jaxlib``
        down there rebuilds the exact outage this file exists to prevent while
        the gate sits green above it.
        """
        runs = [(lineno, text) for lineno, text in _dockerfile_instructions()
                if text.startswith("RUN ")]
        assert runs, "Dockerfile.modal has no RUN instructions at all"
        gates = self._gates()
        assert gates, "no import gate — see the test above"
        last_lineno, last_text = runs[-1]
        assert last_lineno == max(lineno for lineno, _ in gates), (
            f"Dockerfile.modal line {last_lineno} is a RUN instruction placed "
            f"AFTER the build-time import gate: {last_text!r}. The gate only "
            "proves the state of the image as of its own layer; nothing below "
            "it is checked by anything.")


# ---------------------------------------------------------------------------
# The local Modal entrypoints: a console that cannot kill the run
# ---------------------------------------------------------------------------
#
# Every script here is invoked as ``modal run tools/proteina/<file>.py`` from a
# Windows console. Container output arrives through modal's log pump and the
# proteina container prints U+2713, U+1F4CD and box-drawing characters; on a
# cp1252 console the write raises UnicodeEncodeError and kills the LOCAL
# process while the REMOTE container keeps billing. That killed
# ``_hotspot_canary --phase 0`` on 2026-08-04. The fix is to reconfigure
# sys.stdout/sys.stderr IN PLACE before ``import modal`` — in place, because
# modal's log pump, rich and the traceback printer each capture the stream
# object when they start, so replacing sys.stdout leaves all of them writing to
# the strict original.

_HARDENED_ENTRYPOINTS = ("_validate_smoke.py", "_design_canary.py",
                         "seed_volumes.py")

# Stubs modal BEFORE the module is loaded, so nothing here builds an Image,
# contacts Modal or needs the package installed. Then prints upstream's two
# characters (U+2713 and U+1F4CD, verbatim in shape from `complexa target add`)
# at a console that really is cp1252.
_CP1252_ENTRYPOINT_PROBE = r"""
import importlib.util, sys, types

stub = types.ModuleType("modal")


class _Image:
    @staticmethod
    def from_dockerfile(*a, **k):
        return _Image()

    @staticmethod
    def debian_slim(*a, **k):
        return _Image()

    def add_local_file(self, *a, **k):
        return self

    def apt_install(self, *a, **k):
        return self

    def pip_install(self, *a, **k):
        return self

    def env(self, *a, **k):
        return self


class _Volume:
    @staticmethod
    def from_name(*a, **k):
        return _Volume()


class _App:
    def __init__(self, *a, **k):
        pass

    def function(self, *a, **k):
        return lambda fn: fn

    def local_entrypoint(self, *a, **k):
        return lambda fn: fn


class _Function:
    @staticmethod
    def from_name(*a, **k):
        raise AssertionError("the probe must never call out to Modal")


stub.Image, stub.Volume, stub.App, stub.Function = _Image, _Volume, _App, _Function
sys.modules["modal"] = stub

spec = importlib.util.spec_from_file_location("_entrypoint_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["_entrypoint_probe"] = module
spec.loader.exec_module(module)

print("encoding:", sys.stdout.encoding, "errors:", sys.stdout.errors)
print("  ✓ Updated target 'hub_canary0123456789ab'")
print("  📍 Saved to: configs/targets/targets_dict.yaml")
sys.stderr.write("  ✓ on stderr\n")
sys.stdout.flush()
print("EXIT-OK")
"""


@pytest.mark.parametrize("filename", _HARDENED_ENTRYPOINTS)
class TestEntrypointConsoleHardening:

    def test_both_streams_are_hardened_before_modal_is_imported(self, filename):
        """ORDER IS THE POINT and only the source can show it.

        modal is what streams the container's output, so a console it can kill
        is a console it can kill from the first log line onwards. stderr counts
        too: a traceback goes there.
        """
        path = _PROTEINA_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hardened = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("harden_stream", "_harden_stream"):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "sys"):
                    hardened[target.attr] = node.lineno
        assert set(hardened) == {"stdout", "stderr"}, (
            f"{filename} hardens {sorted(hardened)} at module scope; both "
            "streams must be hardened — a traceback goes to stderr")
        modal_imports = [node.lineno for node in ast.walk(tree)
                         if isinstance(node, ast.Import)
                         and any(alias.name == "modal" for alias in node.names)]
        assert modal_imports, (
            f"{filename} no longer imports modal, so this ordering test checks "
            "nothing — delete it or point it at what replaced it")
        assert max(hardened.values()) < min(modal_imports), (
            f"{filename} imports modal before hardening the console")

    def test_the_error_handler_keeps_which_character_failed(self, filename):
        """``backslashreplace``, not ``replace``. A screen of '?' is how a
        cosmetic-looking encoding problem gets ignored until it costs money."""
        src = (_PROTEINA_DIR / filename).read_text(encoding="utf-8")
        assert 'CONSOLE_ERRORS = "backslashreplace"' in src, (
            f"{filename} no longer uses backslashreplace, so the operator can "
            "no longer tell WHICH character the console could not render")

    def test_the_real_module_body_survives_a_cp1252_console(self, filename):
        """END TO END on the REAL file, in a child interpreter whose stdout
        really is cp1252 — the only test here that executes the module scope
        that does the hardening."""
        path = _PROTEINA_DIR / filename
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        proc = subprocess.run(
            [sys.executable, "-c", _CP1252_ENTRYPOINT_PROBE, str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=180)
        assert proc.returncode == 0, (
            f"importing {filename} and printing upstream's line to a cp1252 "
            f"console killed the interpreter:\n{proc.stderr[-2000:]}")
        assert "EXIT-OK" in proc.stdout, proc.stdout
        assert "errors: backslashreplace" in proc.stdout, (
            f"{filename}'s module import did not reconfigure the child's "
            f"stdout: {proc.stdout!r}")
        assert "UnicodeEncodeError" not in proc.stderr, proc.stderr


class TestValidateSmokeIsTargetable:
    """The staging gate has to be able to gate the build being staged."""

    @staticmethod
    def _module_constants(tree):
        return {target.id: node.value.value
                for node in tree.body if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)}

    def _main(self):
        path = _PROTEINA_DIR / "_validate_smoke.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fn = next((node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "main"), None)
        assert fn is not None, "_validate_smoke has no main() entrypoint"
        return tree, fn

    def test_the_app_under_test_is_a_parameter_not_a_constant(self):
        """It hardcoded ``ranomics-proteina-prod``, so on 2026-08-04 it passed
        green against the 2026-07-16 deploy at the same time as a fresh build
        of the same Dockerfile was producing an image where every run died at
        import. A smoke that can only ask about prod cannot answer "did MY
        build break?"."""
        _tree, fn = self._main()
        names = [arg.arg for arg in fn.args.args + fn.args.kwonlyargs]
        assert "app_name" in names, (
            "_validate_smoke.main takes no app_name parameter, so `modal run "
            "... --app-name <staging>` cannot aim it at a candidate deploy; it "
            "can only ever re-test whatever is already in production")

    def test_the_default_target_is_still_prod(self):
        """Defaulting elsewhere would silently turn the prod gate into a
        staging gate."""
        tree, fn = self._main()
        constants = self._module_constants(tree)
        positional = fn.args.args
        offset = len(positional) - len(fn.args.defaults)
        defaults = {arg.arg: default
                    for arg, default in zip(positional[offset:], fn.args.defaults)}
        defaults.update({arg.arg: default for arg, default
                         in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if default})
        node = defaults.get("app_name")
        assert node is not None, "app_name has no default; `modal run` needs one"
        if isinstance(node, ast.Constant):
            value = node.value
        else:
            assert isinstance(node, ast.Name), f"unresolvable default: {ast.dump(node)}"
            value = constants.get(node.id)
        assert value == "ranomics-proteina-prod", (
            f"_validate_smoke defaults to {value!r} rather than the prod app")

    def test_the_docstring_says_what_a_green_run_does_not_prove(self):
        """The whole reason this smoke misled once. Its own docstring has to
        carry the caveat, because that is what the operator reads."""
        src = (_PROTEINA_DIR / "_validate_smoke.py").read_text(encoding="utf-8")
        head = src[:src.index('"""', 3) + 3].lower()
        assert "does not" in head and "deployed" in head, (
            "_validate_smoke's docstring no longer states the limit of what a "
            "green run proves (that it tests the DEPLOYED image, not a fresh "
            "build of Dockerfile.modal)")


class TestJaxDoesNotPreallocateTheCard:
    """The allocator flags are load-bearing for MEASUREMENT, not just for VRAM.

    proteinfoundation.generate imports colabdesign -> JAX, and JAX's default is
    XLA_PYTHON_CLIENT_PREALLOCATE=true at MEM_FRACTION=0.75: the first JAX op
    reserves 0.75 x 81,920 = 61,440 MB on an A100-80GB whatever the target
    size, and holds it. That default silently invalidated both paid canary
    measurements — 67,546 MB at 129 aa and 67,570 MB at 130 aa are ~91% that
    constant, which is why they agreed to 24 MB while the chain count doubled.

    So these are not performance tests. They protect the only route by which
    this tool can ever learn its own size limits. tools/af2 and tools/colabfold
    have set the same flags all along; proteina set none and passed no env= at
    all.
    """

    def test_the_allocator_env_disables_preallocation(self):
        env = rp.design_subprocess_env()
        assert env["XLA_PYTHON_CLIENT_PREALLOCATE"] == "false"
        assert env["XLA_PYTHON_CLIENT_ALLOCATOR"] == "platform"
        assert env["TF_FORCE_GPU_ALLOW_GROWTH"] == "true"

    def test_unified_memory_stays_off_so_an_oversized_run_dies_cheaply(self):
        """Deliberate divergence from af2/colabfold. Host-memory spill turns an
        OOM (seconds, cents) into thrashing that bills to _MAX_SESSION_S
        (~$12.58/shard). For the tool whose open risk is uncapped spend on
        oversized targets, failing fast is worth more than finishing slowly."""
        assert "TF_FORCE_UNIFIED_MEMORY" not in rp._ALLOCATOR_ENV

    def test_an_operator_override_still_wins(self):
        """setdefault, not assignment — so a per-run override is possible
        without editing the file."""
        with patch.dict(os.environ, {"XLA_PYTHON_CLIENT_PREALLOCATE": "true"}):
            assert (rp.design_subprocess_env()
                    ["XLA_PYTHON_CLIENT_PREALLOCATE"] == "true")

    def test_run_streaming_actually_passes_the_env_to_the_child(self):
        """The flags existing in a dict is worth nothing if the subprocess does
        not receive them. run_streaming passed NO env= before this."""
        seen = {}

        def _fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(returncode=0)

        with patch.object(rp.subprocess, "run", _fake_run):
            rp.run_streaming(["echo", "hi"], Path("."))
        env = seen.get("env") or {}
        assert env.get("XLA_PYTHON_CLIENT_PREALLOCATE") == "false", (
            "run_streaming launched the child without the allocator flags")


class TestRuntimeCopyMatchesMeasurement:
    """meta.py's runtime copy is what a user plans and budgets against.

    It shipped claiming "30 to 120" minutes per shard for all three design
    variants. The one paid canary shard that has ever been timed returned 8
    designs in 359 s (6.0 min) at a 130-residue target — the published band
    started 5x above the measurement and ran 20x above it. Worse, it was
    load-bearing beyond the copy: shared/pdb_preflight_rules.py anchored its
    runtime estimator to "the middle of that band", so an invented number in a
    docs constant had propagated into the preflight panel as if calibrated.
    """

    def test_protein_binder_runtime_reflects_the_359_second_shard(self):
        from tools.proteina import meta
        entry = meta.PRESET_RUNTIME["protein_binder"]["typical_minutes"]
        assert "30 to 120" not in entry, (
            "protein_binder still quotes the placeholder band; the measured "
            "shard was 359 s (6.0 min) at a 130-residue target")
        assert "6" in entry

    def test_untimed_variants_are_labelled_untimed(self):
        """ligand_binder and motif_ame have never been run on a GPU here. Their
        copy must say so rather than borrow protein_binder's measurement — the
        reward stacks differ (RF3 vs AF2) and nothing licenses the transfer."""
        from tools.proteina import meta
        for preset in ("ligand_binder", "motif_ame"):
            entry = str(meta.PRESET_RUNTIME[preset]["typical_minutes"]).lower()
            assert "not yet measured" in entry, (
                f"{preset} quotes a runtime nobody has measured")

    def test_about_panel_table_agrees_with_preset_runtime(self):
        """Two copies of the same claim drift, and the drifting one is the one
        the user reads. The about panel and the preset map both render runtime,
        so neither may still carry the retired band."""
        from tools.proteina import meta
        rows = {r["preset"]: r["typical"] for r in meta.about["runtime_table"]}
        assert set(rows) == set(meta.PRESET_RUNTIME)
        for preset, typical in rows.items():
            assert "30 to 120" not in typical, (
                f"about.runtime_table[{preset}] still quotes the placeholder")
        assert "measured" in rows["protein_binder"]

    def test_the_estimator_anchor_is_no_longer_taken_from_this_file(self):
        """The specific coupling that turned a docs placeholder into a number
        the preflight panel presented as calibrated."""
        from shared.pdb_preflight_rules import TOOL_RULES
        base = TOOL_RULES["proteina"].size.runtime_base_min
        assert base != 75.0, (
            "runtime_base_min is still the midpoint of meta.py's retired "
            "30-120 min band")
        # base x (130/120)^1.3 x (8/8) must reproduce the measured 6.0 min.
        env = TOOL_RULES["proteina"].size
        est = base * (130.0 / 120.0) ** env.runtime_alpha
        assert 5.0 <= est <= 7.5, (
            f"the estimator puts the measured 8-design shard at {est:.1f} min, "
            f"not the 6.0 min it actually took")
