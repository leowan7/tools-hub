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
import base64
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

    def test_a_chain_may_repeat_when_the_ranges_are_DISJOINT(self):
        """THE CONTIG A GAPPED TARGET NEEDS, WHICH USED TO BE UN-TYPABLE.

        Upstream resolves every integer between a range's endpoints and raises
        on the first residue the file does not hold, so a chain with a
        disordered loop has to be written ``A1-50,A60-240``. The old rule —
        "Chain A appears more than once … Give each chain a single range" —
        refused exactly that, so a user who KNEW about their gap had no way to
        say so. ``run_pipeline.contig_runs`` now derives the split itself, and
        the form has to agree with the container about what is legal.
        """
        inp, err = px.validate(_custom(target_input="A1-50,A60-240"), {})
        assert err is None, err
        assert inp["target_input"] == "A1-50,A60-240"
        assert inp["_target_segments"] == [("A", 1, 50), ("A", 60, 240)]

    def test_a_repeated_chain_is_named_ONCE_in_target_chain(self):
        """``chain_ids`` becomes ``target_chain`` and the allow-list
        ``_parse_hotspots`` judges a prefix against. A duplicate there renders
        "chain A A" and makes the multi-chain hotspot refusal read "write A241
        or A241" — and, worse, turns a genuinely single-chain run into a
        "targets more than one chain" refusal for every bare hotspot."""
        inp, err = px.validate(
            _custom(target_input="A1-50,A60-240", hotspot_residues="70"), {})
        assert err is None, err
        assert inp["target_chain"] == "A"
        assert inp["hotspot_spec"] == ["A70"], (
            "a bare hotspot stopped being promoted onto the single target "
            "chain, so the repeat is being counted as two chains")

    def test_overlapping_ranges_on_one_chain_are_still_rejected(self):
        for token in ("A1-50,A40-90",      # partial overlap
                      "A1-50,A50-90",      # touching at one residue
                      "A1-19,A1-19",       # the size-floor defeater
                      "A10-20,A1-90"):     # fully contained
            _, err = px.validate(_custom(target_input=token), {})
            assert err and "overlap" in err.lower(), (
                f"{token} was accepted: {err!r}")

    def test_a_bare_chain_id_overlaps_every_range_on_that_chain(self):
        """``A`` is "the whole chain", so it cannot be disjoint from anything
        on A — including a second ``A``."""
        for token in ("A,A1-50", "A1-50,A", "A,A"):
            _, err = px.validate(_custom(target_input=token), {})
            assert err and "overlap" in err.lower(), (
                f"{token} was accepted: {err!r}")

    def test_disjoint_ranges_on_DIFFERENT_chains_are_untouched(self):
        inp, err = px.validate(_custom(target_input="A1-50,B1-50,A60-90"), {})
        assert err is None, err
        assert inp["target_chain"] == "A B"
        assert inp["_target_segments"] == [
            ("A", 1, 50), ("B", 1, 50), ("A", 60, 90)]

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
        """``hotspot_spec`` keeps the prefix; ``hotspot_residues`` is the
        stripped copy.

        The stripped copy is LOSSY ON PURPOSE: it is the shape the shared
        ``hotspot_residues`` key carries fleet-wide — the one launch field
        posted to every selected tool (``_SHARED_LAUNCH_FIELDS`` in
        blueprints/targets.py), which holds plain integers and nothing else.
        Dropping the chain here is safe only because nothing that spends money
        reads it: all four paid gates call
        ``shared.pdb_preflight.shipped_hotspots``, which prefers the spec. See
        tests/test_multichain_targets.py::
        test_shipped_hotspots_prefers_the_spec_and_is_a_no_op_without_one.
        """
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
        # af2folding_plddt is the AfDesign LOSS (1 - pLDDT); af2folding_plddt_log
        # is the metric. Both are written, complementary, as upstream does.
        csv_text = (
            "pdb_path,pdb_index,total_reward,af2folding_i_ptm_log,af2folding_plddt,af2folding_plddt_log,af2folding_rmsd,sample_type,metadata_tag\n"
            f"{sub / 'design_A.pdb'},0,-0.60,0.18,0.38,0.62,5.2,final,design_A\n"
            f"{sub / 'design_B.pdb'},1,-0.45,0.30,0.29,0.71,0.8,final,design_B\n"
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
        # af2folding_plddt_log, NOT the 0.29 loss column beside it.
        assert s["af2_plddt"] == 0.71
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

    def test_one_absent_residue_named_by_both_ends_is_reported_once(
            self, tmp_path):
        """``B443-443`` is lo AND hi, both absent. The docstring promises the
        pair is deduped so the refusal names it once; without the dedup the
        message reads "residue 443 on chain B, residue 443 on chain B". Pinned
        because the docstring makes the claim — an unchecked promise about
        output is the drift this file keeps finding."""
        residues = self._residues(tmp_path)
        assert rp.missing_endpoints(residues, [("B", 443, 443)]) == [("B", 443)]
        # Distinct absent endpoints are still both reported — dedup must not
        # collapse them to the first.
        assert rp.missing_endpoints(residues, [("B", 444, 445)]) == [
            ("B", 444), ("B", 445)]

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
        # ``e.g. `` prefix deliberately: the bare string "B236-442" is ALSO in
        # the spans sentence, so asserting it alone passes with the suggestion
        # stripped out entirely. The advice is the half an operator acts on.
        assert "e.g. B236-442" in detail, "the corrected contig, spelled out"
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


def _gapped_pdb(runs, extra=""):
    """``{chain: [(lo, hi), ...]}`` -> a PDB holding EXACTLY those residues.

    ``_make_pdb`` takes one contiguous span per chain, which is the one shape
    that cannot express the failure this section is about. Every span listed
    here is materialised and nothing between them is, so the file has real
    internal gaps rather than a low occupancy or a missing side chain.
    """
    lines, serial = [], 1
    for chain, spans in runs.items():
        for lo, hi in spans:
            for resseq in range(lo, hi + 1):
                lines.append(_atom(serial, "CA", "ALA", chain, resseq))
                serial += 1
    return "\n".join(lines) + "\n" + extra + "END\n"


class TestContigIsSplitAtDisorderedGaps:
    """THE FAILURE: a range that spans a disordered loop dies on a paid A100.

    Upstream's ``load_target_from_pdb`` resolves the contig through atomworks'
    ``AtomSelectionStack.from_contig``, which expands a range into ONE
    ``AtomSelection`` PER INTEGER, and ``get_mask`` is a bare list comprehension
    over a per-selection mask that raises ``ValueError("No atoms found for
    selection: ...")`` on an empty match. So EVERY residue number between the
    two endpoints has to be in the file — not just the endpoints.

    Most crystal structures have a disordered loop. Nothing before the GPU
    caught it, and each miss was defensible on its own terms:

    * ``select_residues`` filters ``lo <= resseq <= hi``, so the gap is simply
      not selected. The count is right.
    * ``empty_segments`` sees a healthy non-empty selection.
    * ``missing_endpoints`` (PR #118) guards the ENDPOINTS against this exact
      raise — its own message even says "a run is first-to-last and can have
      gaps inside it" — and never looks inside.
    * ``derive_segments`` builds ``(chain, min, max)``, so a BLANK contig
      produces the gap-spanning range by itself.
    * ``complexa target add`` never opens the PDB (pure YAML), so registration
      and read-back cannot see it.
    * the web tier structurally cannot see it: ``chain_summary`` carries a
      per-chain count plus min/max resnum, not the resnum list
      (``shared/targets.py::selection_residue_count`` says so). The container is
      the only place that can decide.

    Comma-separated segments are UNIONED upstream and repeating a chain is
    legal, so ``A1-50,A60-240`` succeeds exactly where ``A1-240`` dies.
    ``contig_runs`` derives that split from the structure.
    """

    # Borrowed, exactly as TestContigEndpointsMustBeRealResidues borrows them.
    _drive = TestCustomTargetRegistration._drive
    _spec = TestCustomTargetRegistration._spec

    # An Fc-like homodimer: two protomers sharing one author numbering, each
    # with a DIFFERENT disordered loop. That asymmetry is the point — a fixture
    # whose chains break in the same place would pass a per-file split as
    # readily as a per-chain one.
    _FC_GAPS = {"A": [(236, 300), (310, 443)], "B": [(236, 350), (360, 442)]}
    _FC_CONTIG = "A236-443,B236-442"
    _FC_SPLIT = "A236-300,A310-443,B236-350,B360-442"

    @classmethod
    def _fc(cls):
        return _gapped_pdb(cls._FC_GAPS)

    @staticmethod
    def _residues(tmp_path, text):
        tmp_path.mkdir(parents=True, exist_ok=True)
        p = tmp_path / "in.pdb"
        p.write_text(text)
        return rp.pdb_ca_residues(p)[0]

    def _prepare(self, tmp_path, monkeypatch, target_input, pdb_text,
                 target_chain="A"):
        """``prepare_custom_target`` with everything outside tmp_path patched.

        Modelled on ``TestMinimumTargetSize._prepare`` and diverging in one
        way: the PDB text is passed in, because every case here needs a
        structure ``_make_pdb``'s one-contiguous-span-per-chain shape cannot
        build. Nothing reaches ``complexa`` — the registry path does not exist,
        which is a DIFFERENT check name and is what makes "got past the gate"
        observable.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        hub = tmp_path / "hub"
        results = tmp_path / "smoke_results.json"
        monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(hub))
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(results))
        monkeypatch.setattr(rp, "_TARGETS_DICT", str(tmp_path / "no_registry.yaml"))
        monkeypatch.setattr(
            rp, "download_target", lambda url, dest: dest.write_text(pdb_text))
        with pytest.raises(SystemExit) as excinfo:
            rp.prepare_custom_target(
                input_url="https://example.invalid/target.pdb", job_id="j1",
                target_chain=target_chain, target_input=target_input,
                hotspot_spec=[], binder_length=[60, 120],
                run_dir=tmp_path / "run")
        assert excinfo.value.code == 1
        return json.loads(results.read_text())["error"], sorted(
            p.name for p in hub.glob("hub_*.pdb"))

    # ---- the pure helper -------------------------------------------------

    def test_a_gapless_segment_comes_back_UNCHANGED(self, tmp_path):
        """THE PROPERTY WORTH PINNING ABOVE ALL THE OTHERS.

        This rewrite is applied to every contig, gappy or not, so the ordinary
        case has to survive it byte for byte. If a gapless range ever came back
        as anything but itself, every existing target would re-register under a
        different key and a different ``--target-input`` for no reason.
        """
        residues = self._residues(tmp_path, _make_pdb({"A": (1, 240)}))
        assert rp.contig_runs(residues, [("A", 1, 240)]) == [("A", 1, 240)]
        assert rp.format_contig(
            rp.contig_runs(residues, rp.parse_target_input("A1-240"))) == "A1-240"

    def test_a_gap_splits_the_segment(self, tmp_path):
        residues = self._residues(tmp_path, _gapped_pdb({"A": [(1, 50), (60, 240)]}))
        assert rp.contig_runs(residues, [("A", 1, 240)]) == [
            ("A", 1, 50), ("A", 60, 240)]
        assert rp.format_contig(
            rp.contig_runs(residues, [("A", 1, 240)])) == "A1-50,A60-240"

    def test_a_one_residue_gap_and_a_one_residue_run_both_split(self, tmp_path):
        """The smallest gap upstream can die on is a single absent residue, and
        the smallest run is a single present one. Both are rendered as ``n-n``,
        which upstream's ``([A-Za-z]+)(\\d+)-(\\d+)`` matches."""
        residues = self._residues(
            tmp_path, _gapped_pdb({"A": [(1, 20), (22, 22), (24, 60)]}))
        assert rp.contig_runs(residues, [("A", 1, 60)]) == [
            ("A", 1, 20), ("A", 22, 22), ("A", 24, 60)]
        assert rp.format_contig(rp.contig_runs(residues, [("A", 1, 60)])) == (
            "A1-20,A22-22,A24-60")

    def test_two_protomers_with_DIFFERENT_gaps_split_differently(self, tmp_path):
        """The homodimer case. A and B share one author numbering and break in
        different places, so a split derived once and applied to both chains —
        or derived from chain A and reused — would be wrong on B."""
        residues = self._residues(tmp_path, self._fc())
        runs = rp.contig_runs(residues, rp.parse_target_input(self._FC_CONTIG))
        assert runs == [("A", 236, 300), ("A", 310, 443),
                        ("B", 236, 350), ("B", 360, 442)]
        assert rp.format_contig(runs) == self._FC_SPLIT

    def test_a_gapless_chain_beside_a_gapped_one_is_left_alone(self, tmp_path):
        """Multi-chain, mixed. Only the chain that needs splitting is split."""
        residues = self._residues(
            tmp_path, _gapped_pdb({"A": [(1, 50), (60, 240)], "B": [(1, 100)]}))
        assert rp.contig_runs(
            residues, rp.parse_target_input("A1-240,B1-100")) == [
                ("A", 1, 50), ("A", 60, 240), ("B", 1, 100)]

    def test_a_bare_chain_id_means_the_whole_chain(self, tmp_path):
        """``(chain, None, None)`` is legal input to every other predicate here
        and must not raise. It selects the whole chain, split at its gaps —
        the same reading ``select_residues`` gives it."""
        residues = self._residues(tmp_path, _gapped_pdb({"A": [(1, 50), (60, 240)]}))
        assert rp.contig_runs(residues, [("A", None, None)]) == [
            ("A", 1, 50), ("A", 60, 240)]

    def test_insertion_codes_do_not_split_a_run(self, tmp_path):
        """``A100`` and ``A100A`` are two residues with two CA atoms but ONE
        number, and a contig endpoint is a bare integer with nowhere to put a
        code. Runs are computed over distinct ``resseq``, matching
        ``missing_endpoints``, so a coded twin neither splits a run nor bridges
        a gap."""
        text = "\n".join([
            _atom(1, "CA", "ALA", "A", 100),
            _atom(2, "CA", "ALA", "A", 100, icode="A"),
            _atom(3, "CA", "ALA", "A", 101),
            _atom(4, "CA", "ALA", "A", 103),
        ]) + "\nEND\n"
        residues = self._residues(tmp_path, text)
        assert residues == [("A", 100, ""), ("A", 100, "A"), ("A", 101, ""),
                            ("A", 103, "")], "fixture check"
        assert rp.contig_runs(residues, [("A", 100, 103)]) == [
            ("A", 100, 101), ("A", 103, 103)]

    def test_a_chain_absent_from_the_file_contributes_no_run(self, tmp_path):
        """Unreachable from production — ``empty_segments`` refuses it first —
        but the helper is pure and must answer rather than raise."""
        residues = self._residues(tmp_path, _make_pdb({"A": (1, 60)}))
        assert rp.contig_runs(residues, [("Z", 1, 99)]) == []
        assert rp.contig_runs(residues, [("A", 1, 60), ("Z", 1, 99)]) == [
            ("A", 1, 60)]

    def test_segment_order_is_kept_and_overlaps_are_not_merged(self, tmp_path):
        """Upstream ORs the per-selection masks, so a residue named twice is
        selected once either way. Merging across segments would break the
        one-to-one correspondence with the segments the guards judged."""
        residues = self._residues(tmp_path, _make_pdb({"A": (1, 60), "B": (1, 40)}))
        assert rp.contig_runs(residues, [("B", 1, 40), ("A", 1, 60)]) == [
            ("B", 1, 40), ("A", 1, 60)], "segment order"
        assert rp.contig_runs(residues, [("A", 1, 30), ("A", 20, 60)]) == [
            ("A", 1, 30), ("A", 20, 60)], "overlaps stay two runs"

    def test_the_crop_selects_the_SAME_residues_either_way(self, tmp_path):
        """VERIFIED, NOT ASSUMED. ``selected_residue_keys`` is what
        ``stage_cropped_target`` writes, and the claim that it needs no change
        is only true if the split selects the identical set. Asserted on the
        keys AND through the staged file's own self-check, which is the number
        upstream compares."""
        text = self._fc()
        residues = self._residues(tmp_path, text)
        segments = rp.parse_target_input(self._FC_CONTIG)
        runs = rp.contig_runs(residues, segments)
        assert (rp.selected_residue_keys(residues, segments)
                == rp.selected_residue_keys(residues, runs))
        assert (rp.stage_cropped_target(tmp_path / "a.pdb", text, residues, segments)
                == rp.stage_cropped_target(tmp_path / "b.pdb", text, residues, runs))
        assert ((tmp_path / "a.pdb").read_text() == (tmp_path / "b.pdb").read_text())

    # ---- the rendered --target-input --------------------------------------

    def test_the_registered_contig_carries_the_split(self, tmp_path, monkeypatch):
        """THE MONEY ASSERTION, END TO END THROUGH ``main()``. The contig that
        reaches ``complexa target add`` is the one upstream will resolve."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._FC_CONTIG, target_chain="A B"),
            calls=calls, pdb_text=self._fc())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == self._FC_SPLIT

    def test_a_blank_contig_is_split_too(self, tmp_path, monkeypatch):
        """``derive_segments`` emits ``(chain, min, max)`` — ONE span per chain
        — so the no-contig path produces the gap-spanning range all by itself.
        That is the shape most users hit, since the field is optional."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="", target_chain="A B"),
            calls=calls, pdb_text=self._fc())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == self._FC_SPLIT
        # ...and the derivation really did produce the un-split range, so this
        # test cannot pass because the fixture happens to be gapless.
        residues = self._residues(tmp_path, self._fc())
        assert rp.format_contig(
            rp.derive_segments(residues, ["A", "B"])) == self._FC_CONTIG

    def test_target_input_stays_ONE_argv_element(self, tmp_path, monkeypatch):
        """``--target-input`` is a plain argparse option, NOT ``nargs="+"`` —
        unlike ``--hotspot-residues`` and ``--binder-length`` beside it. The
        split introduces commas into a value that previously often had none, so
        the shape it relies on is pinned here rather than assumed: one element,
        commas and all, and the next element is the following FLAG."""
        calls: list = []
        self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._FC_CONTIG, target_chain="A B"),
            calls=calls, pdb_text=self._fc())
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        i = add.index("--target-input")
        assert add[i + 1] == self._FC_SPLIT
        assert add[i + 2].startswith("--"), (
            "the contig was split across argv elements; argparse would take "
            f"only the first: {add[i + 1:i + 4]}")
        assert add.count(self._FC_SPLIT) == 1

    def test_the_registry_readback_compares_the_SPLIT_string(
            self, tmp_path, monkeypatch):
        """One variable feeds the record, the read-back comparison and the CLI
        flag. If the record kept the un-split form, ``registration_mismatch``
        would refuse a registration that had actually succeeded."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input=self._FC_CONTIG, target_chain="A B"),
            calls=calls, pdb_text=self._fc())
        assert data["status"] != "FAILED", data.get("error")
        registry = (tmp_path / "proteina" / "configs" / "targets"
                    / "targets_dict.yaml").read_text()
        assert f"target_input: {self._FC_SPLIT}" in registry, registry

    def test_a_gapless_upload_registers_EXACTLY_as_before(
            self, tmp_path, monkeypatch):
        """THE CONTROL. ``_make_3s7g_like`` has no internal gaps, so the whole
        rewrite must be invisible on it — same contig, one segment per chain."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="A236-443,B236-442", target_chain="A B"),
            calls=calls, pdb_text=_make_3s7g_like())
        assert data["status"] != "FAILED", data.get("error")
        add = next(c for c in calls if c[:3] == [rp.COMPLEXA_BIN, "target", "add"])
        assert add[add.index("--target-input") + 1] == "A236-443,B236-442"

    def test_the_rewrite_is_LOGGED_when_it_changes_anything(
            self, tmp_path, monkeypatch, caplog):
        """The operator reads the shard log to find out what was designed
        against. A silent rewrite of the contig they typed is the kind of
        helpfulness that becomes a mystery three weeks later."""
        with caplog.at_level("INFO", logger="proteina_pipeline"):
            self._prepare(tmp_path, monkeypatch, self._FC_CONTIG, self._fc(),
                          target_chain="A B")
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert self._FC_CONTIG in text and self._FC_SPLIT in text, text
        assert "4 contiguous run(s)" in text, text

    def test_a_gapless_contig_logs_no_rewrite(self, tmp_path, monkeypatch, caplog):
        """The other half: the line must not appear when nothing changed, or it
        is noise on every run and stops being read."""
        with caplog.at_level("INFO", logger="proteina_pipeline"):
            self._prepare(tmp_path, monkeypatch, "A1-60", _make_pdb({"A": (1, 60)}))
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "contiguous run(s)" not in text, text

    # ---- normalisation must not swallow a guard --------------------------

    def test_missing_endpoints_STILL_fires_on_a_gapped_upload(
            self, tmp_path, monkeypatch):
        """THE ORDERING, AND WHY IT IS THIS WAY ROUND. ``A236-500`` names a
        residue the file does not hold. Normalising first would narrow it to
        the real last residue and swallow a refusal the operator decided to
        keep — the user might have uploaded the wrong file. The rewrite
        therefore runs BELOW every guard, and this is the test that says so."""
        error, staged = self._prepare(
            tmp_path, monkeypatch, "A236-500", self._fc())
        assert error["check"] == "target_input_endpoint", error
        assert "residue 500 on chain A" in error["detail"]
        assert staged == [], "nothing may be staged once an endpoint is absent"

    def test_every_other_refusal_still_fires_with_its_existing_message(
            self, tmp_path, monkeypatch):
        """One gapped structure, four guards, four unchanged verdicts. A
        rewrite placed above any of them would turn one of these green."""
        gapped = _gapped_pdb({"A": [(1, 50), (60, 240)]})
        # step 3b: negative numbering (unrenderable_segments)
        tagged = _gapped_pdb({"A": [(-5, 50), (60, 240)]})
        error, _ = self._prepare(tmp_path / "neg", monkeypatch, "", tagged)
        assert error["check"] == "target_input_negative", error
        assert "A-5-240" in error["detail"], error["detail"]
        # step 4: a segment that selects nothing
        error, _ = self._prepare(tmp_path / "empty", monkeypatch,
                                 "A1-240,Z1-50", gapped)
        assert error["check"] == "target_input", error
        assert "chain Z residues 1-50 select 0 residues" in error["detail"]
        # the size floor, counted on DISTINCT residues
        error, _ = self._prepare(tmp_path / "small", monkeypatch, "A1-10", gapped)
        assert error["check"] == "target_input", error
        assert "Widen the chain range" in error["detail"]
        # step 5: a hotspot inside the gap exists nowhere
        hub = tmp_path / "hot" / "hub"
        results = tmp_path / "hot" / "res.json"
        monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(hub))
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(results))
        monkeypatch.setattr(
            rp, "download_target", lambda url, dest: dest.write_text(gapped))
        with pytest.raises(SystemExit):
            rp.prepare_custom_target(
                input_url="https://example.invalid/t.pdb", job_id="j1",
                target_chain="A", target_input="A1-240", hotspot_spec=["A55"],
                binder_length=[60, 120], run_dir=tmp_path / "hot" / "run")
        error = json.loads(results.read_text())["error"]
        assert error["check"] == "hotspot_missing", error
        assert "A55" in error["detail"]

    def test_the_negative_guard_reads_the_UNREWRITTEN_span(
            self, tmp_path, monkeypatch):
        """The sharpest ordering case. On a construct numbered from -5 with a
        gap, the rewrite would render ``A-5-50,A60-240`` — still unrenderable,
        but the refusal names the SPAN the user asked for. Pinning the message
        pins the order."""
        error, _ = self._prepare(
            tmp_path, monkeypatch, "", _gapped_pdb({"A": [(-5, 50), (60, 240)]}))
        assert error["check"] == "target_input_negative", error
        assert "A-5-240 uses negative residue numbers" in error["detail"]

    def test_the_guards_are_still_called_ABOVE_the_rewrite(self):
        """STRUCTURAL, because the behavioural tests above each cover one
        ordering and a future edit could move the rewrite past a guard they do
        not exercise. Asserted on the source order of the calls inside
        ``prepare_custom_target``."""
        source = Path(rp.__file__).read_text(encoding="utf-8")
        prepare = next(
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.FunctionDef) and n.name == "prepare_custom_target")
        # MIN, not setdefault: ast.walk is breadth-first, so the first node it
        # yields for a name is not the first one in the source. And
        # ``contig_runs`` is deliberately called twice — once to build the
        # endpoint refusal's hint, once to render the contig — so its EARLIER
        # call cannot be the thing the guards are ordered against.
        first: dict[str, int] = {}
        for node in ast.walk(prepare):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                first[node.func.id] = min(
                    first.get(node.func.id, node.lineno), node.lineno)
        assert "contig_runs" in first, (
            "prepare_custom_target must ASK for the split, not restate it")
        rendered = [n for n in ast.walk(prepare) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "contig"
                            for t in n.targets)]
        assert len(rendered) == 1, (
            "the contig that goes to --target-input is rendered in more than "
            "one place, so the ordering pinned here speaks for only one of them")
        assert ast.unparse(rendered[0].value) == "format_contig(runs)", (
            "the registered contig no longer comes from the split")
        for guard in ("unrenderable_segments", "empty_segments",
                      "missing_endpoints", "target_too_small",
                      "missing_hotspots"):
            assert first[guard] < rendered[0].lineno, (
                f"{guard} now runs BELOW the contig rewrite, so it judges a "
                "contig the user never typed")

    # ---- the suggested fix must itself be typable ------------------------

    def test_the_endpoint_refusals_hint_is_split_at_the_gaps(
            self, tmp_path, monkeypatch):
        """PR #118's hint was ``at_or_above[0]``..``at_or_below[-1]`` — two
        endpoints that exist with a span between them that can still straddle a
        gap. We were telling the user to type a range that dies in exactly the
        way we had just refused theirs for."""
        error, _ = self._prepare(tmp_path, monkeypatch, "A236-500", self._fc())
        assert error["check"] == "target_input_endpoint", error
        assert "e.g. A236-300,A310-443" in error["detail"], error["detail"]

    def test_the_hint_it_gives_is_one_the_gate_then_accepts(
            self, tmp_path, monkeypatch):
        """A guard that refuses with unusable advice is a dead end. The
        recommended contig must clear every guard AND already be normalised, so
        pasting it back changes nothing."""
        residues = self._residues(tmp_path, self._fc())
        hint = rp.parse_target_input("A236-300,A310-443")
        assert rp.missing_endpoints(residues, hint) == []
        assert rp.empty_segments(residues, hint) == []
        assert not rp.target_too_small(residues, hint)
        assert rp.contig_runs(residues, hint) == hint, "already normalised"

    def test_a_multi_chain_hint_stays_a_valid_contig(self, tmp_path, monkeypatch):
        """The per-chain hints are comma-joined, and each may now itself hold
        commas. The result still has to parse as one contig."""
        error, _ = self._prepare(
            tmp_path, monkeypatch, "A236-500,B236-500", self._fc(),
            target_chain="A B")
        assert error["check"] == "target_input_endpoint", error
        match = re.search(r"e\.g\. ([A-Za-z0-9,\-]+)", error["detail"])
        assert match, error["detail"]
        assert rp.parse_target_input(match.group(1)) == [
            ("A", 236, 300), ("A", 310, 443), ("B", 236, 350), ("B", 360, 442)]

    # ---- ...and short enough that the BROWSER cannot cut it down ----------
    #
    # THE HOLE THIS BRANCH OPENED, AND THE ONLY DEFENCE THERE IS. Before the
    # hint went through ``contig_runs`` it was one range per chain — never more
    # than ~20 characters — and a one-chain multi-segment contig was un-typable
    # anyway, because the adapter refused a repeated chain. Both of those
    # changed on this branch at once, so the refusal now prints a contig whose
    # length is set by how gappy the structure is, into a field that was capped
    # at 64 characters.
    #
    # A chain with 12 gaps produced a 100-character hint. The browser kept the
    # first 64, the cut happened to land on a comma, and what was left parsed
    # as a perfectly valid 8-segment contig that every gate accepts and stages:
    # 120 residues requested, 80 designed against, nothing anywhere saying so.
    # That asymmetry is what makes this worth a section of its own — a
    # TRUNCATED CONTIG IS STILL A SYNTACTICALLY VALID CONTIG, so no gate
    # downstream can tell it apart from what the operator meant, and the only
    # place the difference is knowable is here, before the string is printed.

    @staticmethod
    def _widest(n_runs, first=1000, step=20, span=10):
        """``n_runs`` runs whose contig text is as wide as a run can ever be.

        ``A1000-1010`` is 10 characters and nothing can beat it: the chain id
        is one letter (``_SEGMENT_RE`` is ``[A-Za-z]``) and a residue number is
        at most four characters, because ``pdb_ca_residues`` reads
        ``line[22:26]`` — four columns — so ``9999`` and ``-999`` are the
        widest values that can come out of a file at all.
        """
        return {"A": [(first + step * i, first + step * i + span)
                      for i in range(n_runs)]}

    def test_a_hint_too_gappy_to_type_is_not_printed_AT_ALL(
            self, tmp_path, monkeypatch):
        """NOT A PREFIX. A shortened run list is a smaller target, and printing
        one that LOOKS complete is worse than printing none: the operator
        pastes it, every gate accepts it, and the run designs against a region
        nobody asked for. The refusal says how many runs there were and sends
        them to narrow the range instead."""
        error, staged = self._prepare(
            tmp_path, monkeypatch, "A1000-9999", _gapped_pdb(self._widest(12)))
        assert error["check"] == "target_input_endpoint", error
        assert "e.g." not in error["detail"], (
            f"a hint of 12 runs was printed anyway: {error['detail']}")
        assert "12 separate runs" in error["detail"], error["detail"]
        assert "narrow the target chain range" in error["detail"].lower(), (
            error["detail"])
        assert staged == []

    def test_the_widest_hint_we_can_print_fits_the_field_and_re_parses(
            self, tmp_path, monkeypatch):
        """THE BOUND, MEASURED RATHER THAN ASSUMED, on the worst input that can
        reach it: ``MAX_HINT_RUNS`` runs of four-digit residue numbers. The
        string that comes out has to fit the form field AND still be a contig
        ``validate()`` accepts unchanged."""
        error, _ = self._prepare(
            tmp_path, monkeypatch, "A1000-9999",
            _gapped_pdb(self._widest(rp.MAX_HINT_RUNS)))
        assert error["check"] == "target_input_endpoint", error
        match = re.search(r"e\.g\. ([A-Za-z0-9,\-]+)", error["detail"])
        assert match, error["detail"]
        hint = match.group(1)
        assert hint.count(",") == rp.MAX_HINT_RUNS - 1, hint
        assert len(hint) == 87, (
            f"the widest hint is {len(hint)} characters, not the 87 the field "
            f"width is derived from: {hint}")
        assert len(hint) <= px._MAX_TARGET_INPUT_FIELD, (
            f"the hint is wider than the field it must be pasted into: {hint}")
        inp, err = px.validate(_custom(target_input=hint), {})
        assert err is None, f"{hint} -> {err}"
        assert inp["target_input"] == hint, "the hint did not survive the form"

    def test_the_bound_counts_runs_across_the_WHOLE_hint_not_per_chain(
            self, tmp_path, monkeypatch):
        """The per-chain hints are comma-joined into ONE contig, and it is that
        contig the form has to accept. Two chains of five runs each is a
        ten-range suggestion — past ``_MAX_SEGMENTS`` — while neither half is,
        so a bound applied per chain prints something the form refuses and the
        browser then cuts. Single-chain fixtures cannot tell the two apart."""
        gaps = {"A": [(1000 + 20 * i, 1010 + 20 * i) for i in range(5)],
                "B": [(1000 + 20 * i, 1010 + 20 * i) for i in range(5)]}
        error, _ = self._prepare(
            tmp_path, monkeypatch, "A1000-9999,B1000-9999", _gapped_pdb(gaps),
            target_chain="A B")
        assert error["check"] == "target_input_endpoint", error
        assert "e.g." not in error["detail"], (
            f"a ten-run hint was printed per chain: {error['detail']}")
        assert "10 separate runs" in error["detail"], error["detail"]

    def test_the_hint_bound_is_the_number_the_FORM_accepts(self):
        """``MAX_HINT_RUNS`` is not a second ``MAX_CONTIG_RUNS``. That one
        bounds what this service will REGISTER — a question about the structure
        — and sits at 64. This one bounds what we PRINT, and the only thing
        that can settle it is how many ranges the form will take back."""
        assert rp.MAX_HINT_RUNS == px._MAX_SEGMENTS, (
            "the hint may now be longer than the form will accept")
        assert rp.MAX_HINT_RUNS <= rp.MAX_CONTIG_RUNS

    def test_the_field_is_wide_enough_for_ANY_hint_the_bound_allows(self):
        """THE ARITHMETIC, WRITTEN OUT, because the field width is a derived
        number and a derived number with no derivation rots into a guess.

        A run renders as ``<letter><lo>-<hi>``. The letter is one character;
        ``lo`` and ``hi` are at most four each (``pdb_ca_residues`` reads
        ``line[22:26]``, so ``9999`` and ``-999`` are the widest a file can
        express); the hyphen is one. ``MAX_HINT_RUNS`` of those, comma-joined,
        is the longest string this code can ever ask a user to paste — and it
        is also the longest contig ``validate()`` would accept from them, since
        ``_MAX_SEGMENTS`` is the same number.
        """
        widest_run = 1 + 4 + 1 + 4
        widest = rp.MAX_HINT_RUNS * widest_run + (rp.MAX_HINT_RUNS - 1)
        assert widest == 87, widest
        assert px._MAX_TARGET_INPUT_FIELD >= widest, (
            f"the field holds {px._MAX_TARGET_INPUT_FIELD} characters and a "
            f"legal contig can be {widest}; the browser would truncate it")

    def test_an_absurdly_long_contig_is_REFUSED_rather_than_raised(self):
        """THE SERVER-SIDE HALF, which a maxlength cannot do: ``maxlength`` is
        an affordance in a browser and nothing at all to curl.

        Not merely tidiness. ``_SEGMENT_RE``'s ``(-?\\d+)`` is unbounded and
        ``_parse_target_input`` calls ``int()`` on what it captures, and since
        Python 3.11 ``int()`` REFUSES a string over 4300 digits — so a posted
        ``A1-<5000 nines>`` came back as an unhandled ``ValueError`` out of
        ``validate()`` rather than as a message, i.e. a 500 on the submit
        route. The length check runs before the regex loop, so the digits are
        never converted.
        """
        _, err = px.validate(_custom(target_input="A1-" + "9" * 5000), {})
        assert err and "too long" in err.lower(), err

    # ---- the run-count ceiling -------------------------------------------

    @staticmethod
    def _alternating(n_runs):
        """A chain of ``n_runs`` single-residue runs: 1, 3, 5, ... Both
        endpoints exist and the selection is well above the size floor, so
        every cheaper guard passes and only the ceiling can fire."""
        return {"A": [(2 * i + 1, 2 * i + 1) for i in range(n_runs)]}

    def test_above_the_ceiling_the_run_is_refused_and_nothing_is_staged(
            self, tmp_path, monkeypatch):
        over = rp.MAX_CONTIG_RUNS + 2
        spans = self._alternating(over)
        error, staged = self._prepare(
            tmp_path, monkeypatch, "", _gapped_pdb(spans))
        assert error["check"] == "target_input_runs", error
        assert f"covers {over} separate runs" in error["detail"], error["detail"]
        assert f"more than the {rp.MAX_CONTIG_RUNS}" in error["detail"]
        # The target's real spans, so the operator can pick a narrower region.
        assert f"A1-{2 * over - 1}" in error["detail"], error["detail"]
        assert staged == [], "a refused target must not be staged"

    def test_the_ceiling_does_NOT_truncate(self, tmp_path, monkeypatch):
        """SILENT TRUNCATION IS THE WORSE BUG. A contig cut to its first N runs
        is a different target, and designing against one nobody asked for is
        the failure class every guard in this file exists to stop. Pinned by
        the absence of any registration at all."""
        calls: list = []
        data = self._drive(
            rp, tmp_path, monkeypatch,
            self._spec(target_input="", target_chain="A"), calls=calls,
            pdb_text=_gapped_pdb(self._alternating(rp.MAX_CONTIG_RUNS + 2)))
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "target_input_runs"
        assert calls == [], "no subprocess may run once the ceiling is exceeded"

    def test_at_the_ceiling_the_run_gets_past_the_gate(
            self, tmp_path, monkeypatch):
        """The control, and the reason the bound is ``>`` rather than ``>=``.
        It still fails — the registry does not exist here — but on the check
        that comes AFTER the crop."""
        error, staged = self._prepare(
            tmp_path, monkeypatch, "",
            _gapped_pdb(self._alternating(rp.MAX_CONTIG_RUNS)))
        assert error["check"] == "target_registry", error
        assert len(staged) == 1, f"never reached the crop: {error}"

    def test_the_guard_actually_READS_the_constant(self, tmp_path, monkeypatch):
        """THE CONSTANT MUST GOVERN, NOT MERELY AGREE — the mutation that
        survived on ``MIN_SELECTED_RESIDUES`` was a hardcoded literal matching
        the constant's current value. Moving the number must move the answer,
        asserted in both directions so it cannot pass on the fixture's size."""
        # 24 runs: above MIN_SELECTED_RESIDUES so the size floor cannot fire
        # first, and well under the real ceiling so only the patch decides.
        text = _gapped_pdb(self._alternating(24))
        residues = self._residues(tmp_path, text)
        assert len(rp.contig_runs(residues, [("A", 1, 47)])) == 24
        assert not rp.target_too_small(residues, [("A", 1, 47)]), (
            "the fixture must clear the size floor or that guard, not this "
            "one, is what these two assertions are measuring")

        monkeypatch.setattr(rp, "MAX_CONTIG_RUNS", 4)
        error, _ = self._prepare(tmp_path / "lo", monkeypatch, "", text)
        assert error["check"] == "target_input_runs", (
            "lowering the ceiling below the run count must refuse it; the "
            "guard is not reading MAX_CONTIG_RUNS")
        assert "more than the 4" in error["detail"], error["detail"]

        monkeypatch.setattr(rp, "MAX_CONTIG_RUNS", 40)
        error, staged = self._prepare(tmp_path / "hi", monkeypatch, "", text)
        assert error["check"] == "target_registry", (
            "raising the ceiling above the run count must accept it; the "
            "guard is not reading MAX_CONTIG_RUNS")
        assert len(staged) == 1

    def test_the_ceiling_is_labelled_uncalibrated(self):
        """THE PROVENANCE CLAIM, PINNED WHERE IT CAN ROT — the same convention
        ``MIN_SELECTED_RESIDUES`` and ``SizeEnvelope.cap_basis`` follow. No
        structure has been measured against this number and no upstream limit
        implies it; a constant that quietly loses its label reads as measured.
        """
        source = Path(rp.__file__).read_text(encoding="utf-8")
        declaration = source.index("MAX_CONTIG_RUNS = ")
        preamble = source[max(0, declaration - 2200):declaration]
        assert "UNCALIBRATED" in preamble, (
            "the ceiling's comment no longer says the number is unmeasured")
        assert "POLICY" in preamble, (
            "the ceiling's comment no longer says the number is a choice")

    def test_the_ceiling_leaves_room_above_the_typed_segment_cap(self):
        """A CONSISTENCY BOUND, not a calibration. The adapter lets a user type
        ``_MAX_SEGMENTS`` ranges; the container splits each of them at every
        gap. A ceiling at or below the typed cap would refuse contigs the form
        had just accepted, after the campaign existed."""
        assert rp.MAX_CONTIG_RUNS > px._MAX_SEGMENTS


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


class TestMinimumTargetSize:
    """The floor below which a target is refused before any GPU is started.

    ``prepare_custom_target`` has always refused a selection under 20 residues —
    the stated reason is that there is not enough surface there to place a
    60-120 residue binder, which is UNCALIBRATED and marked as such at the
    constant — but the threshold was a bare literal inside that function and
    NOTHING covered it. Two consequences, and this class exists for the second
    as much as the first: the check could be deleted with the suite still green,
    and ``_hotspot_canary`` could not call it, so the harness had no floor at
    all and ``--contig A10-20`` would spend ~$4 (phase 1) or ~$12 (phase 2)
    learning what a length knows for free.

    IT COUNTS DISTINCT RESIDUES, WHICH IS THE HALF THAT SHIPPED BROKEN.
    ``target_too_small`` first took whatever selection a caller handed it and
    measured ``len``; ``select_residues`` appends per segment and never
    de-duplicates, so ``A10-20,A10-20`` counted 22 for the same 11 residues and
    cleared a floor of 20. It now takes ``(residues, segments)`` and counts the
    de-duplicated key set the crop actually stages, so there is no collection a
    caller can pass that gives the wrong answer.

    Every assertion below is written against ``rp.MIN_SELECTED_RESIDUES`` rather
    than against 20, so moving the threshold moves the tests with it. That is
    the property being pinned: ONE number, read by both sides.
    """

    @staticmethod
    def _sel(n, chain="A"):
        """``(residues, segments)`` selecting exactly ``n`` residues of a chain."""
        return ([(chain, i, "") for i in range(1, n + 1)],
                [(chain, 1, max(n, 1))])

    def test_a_selection_under_the_floor_is_refused(self):
        floor = rp.MIN_SELECTED_RESIDUES
        assert rp.target_too_small(*self._sel(floor - 1))
        assert rp.target_too_small([], [("A", 1, 60)])

    def test_a_selection_at_the_floor_is_accepted(self):
        """The bound is ``<``, not ``<=``. Off by one here refuses a target the
        engine would have designed against, which is the same class of harm in
        the other direction."""
        floor = rp.MIN_SELECTED_RESIDUES
        assert not rp.target_too_small(*self._sel(floor))
        assert not rp.target_too_small(*self._sel(floor + 40))

    def test_the_floor_is_a_real_floor(self):
        """A sanity bound on the constant itself. A threshold of 0 or 1 would
        make the predicate vacuous and every test above pass on nothing."""
        assert rp.MIN_SELECTED_RESIDUES >= 10

    def test_an_overlapping_contig_does_not_inflate_the_count(self):
        """THE MONEY DEFECT, AT THE PREDICATE. ``select_residues`` repeats a
        residue two segments both name. Naming the same sliver twice must not
        make it twice as big — one comma was the entire bypass."""
        floor = rp.MIN_SELECTED_RESIDUES
        residues = [("A", i, "") for i in range(1, 61)]
        half = [("A", 1, floor - 1)]
        assert len(rp.select_residues(residues, half * 2)) == 2 * (floor - 1), (
            "the fixture must actually double-count, or this proves nothing")
        assert rp.n_selected_residues(residues, half * 2) == floor - 1
        assert rp.target_too_small(residues, half * 2), (
            "a sliver named twice cleared the floor on a doubled count")
        assert rp.target_too_small(residues, [("A", 1, 7)] * 3)

    def test_the_count_is_the_one_the_crop_stages(self):
        """Why DISTINCT is the right count and not merely the smaller one: the
        gate has to measure the file the design engine is handed, and
        ``stage_cropped_target`` writes ``selected_residue_keys``."""
        residues = [("A", i, "") for i in range(1, 61)]
        segments = [("A", 1, 30), ("A", 20, 40)]
        assert (rp.n_selected_residues(residues, segments)
                == len(rp.selected_residue_keys(residues, segments)) == 40)

    def test_a_two_chain_selection_is_counted_across_both_chains(self):
        """THE OVER-REFUSAL DIRECTION, ON THE INPUT SHAPE #109 JUST ENABLED.

        Two near-miss counts both pass every single-chain test and both REFUSE a
        legitimate multi-chain target: counting only the first segment's chain,
        and counting distinct residue NUMBERS chain-blind. Each sees
        ``floor - 1`` where there are ``2 * (floor - 1)`` residues, and every
        fixture in this file used to be single-chain, so nothing could tell them
        apart from the correct predicate.
        """
        floor = rp.MIN_SELECTED_RESIDUES
        hi = floor - 1
        residues = ([("A", i, "") for i in range(1, hi + 1)]
                    + [("B", i, "") for i in range(1, hi + 1)])
        segments = [("A", 1, hi), ("B", 1, hi)]
        assert rp.n_selected_residues(residues, segments) == 2 * hi
        assert len({r for _c, r in rp.select_residues(residues, segments)}) == hi, (
            "the fixture must be chain-blind-ambiguous, or this proves nothing")
        assert not rp.target_too_small(residues, segments), (
            "a legitimate two-chain target was refused; the count is per chain")

    def test_insertion_coded_twins_are_two_residues_not_one(self):
        """THE NEAREST MISS OF ALL: ``len(set(selected))``.

        ``select_residues`` drops the insertion code, so a set of ITS output
        collapses ``A100`` and ``A100A`` into one. They are two residues with
        two CA atoms — upstream counts both and the crop stages both — so a gate
        built on that set refuses a target the design engine would have accepted
        whenever insertion codes bring it to the floor. ``selected_residue_keys``
        is the set that keeps them apart, which is why it is the one that counts.
        """
        floor = rp.MIN_SELECTED_RESIDUES
        plain = [("A", i, "") for i in range(1, floor - 1)]
        twins = [("A", 1, "A"), ("A", 2, "A")]
        residues = plain + twins
        segments = [("A", 1, floor)]
        assert len({r for _c, r in rp.select_residues(residues, segments)}) == floor - 2
        assert rp.n_selected_residues(residues, segments) == floor
        assert not rp.target_too_small(residues, segments), (
            "insertion-coded twins were collapsed and a target at the floor "
            "was refused")

    def test_the_floor_is_labelled_uncalibrated(self):
        """THE PROVENANCE CLAIM, PINNED WHERE IT CAN ROT.

        Nothing has measured this number: it entered as a bare literal, no A100
        run has been made at, above or below it, and the stated rationale about
        binder surface is plausible and is not evidence. The repo already has a
        convention for exactly this (``SizeEnvelope.cap_basis``: "untested" =
        the copy must claim a precaution, not a predicted failure point), and a
        constant that quietly loses its label reads as measured.
        """
        source = Path(rp.__file__).read_text(encoding="utf-8")
        declaration = source.index("MIN_SELECTED_RESIDUES = ")
        preamble = source[max(0, declaration - 1600):declaration]
        assert "UNCALIBRATED" in preamble, (
            "the floor's comment no longer says the number is unmeasured")

    def test_the_floor_stays_below_the_preflight_minimum_it_sits_behind(self):
        """AN UPPER BOUND WITH A REASON, to go with the ``>= 10`` lower one.

        ``shared/pdb_preflight_rules.py`` already refuses a whole named chain
        under ``min_target_aa`` on the submit route. A contig floor ABOVE that
        would make the container stricter than the gate that feeds it: a target
        the preflight blessed would be refused after the job was accepted, which
        is the worst place to learn it. Not a calibration — a consistency bound.
        """
        from shared.pdb_preflight_rules import TOOL_RULES
        assert rp.MIN_SELECTED_RESIDUES <= TOOL_RULES["proteina"].min_target_aa

    def test_the_predicate_actually_READS_the_constant(self, monkeypatch):
        """THE CONSTANT MUST GOVERN, NOT MERELY AGREE.

        Found by mutation after the round-2 fixes: replacing the predicate's
        ``< MIN_SELECTED_RESIDUES`` with a literal ``< 20`` left all 658 tests
        green. Every other test here pins the floor's VALUE or its BEHAVIOUR at
        the current number, and both are satisfied by a hardcoded 20 while the
        constant reads 20 — so the one thing nothing checked was whether the
        constant is wired to anything at all.

        That is this branch's own failure mode aimed at its own fix. The whole
        point of lifting the literal out of ``prepare_custom_target`` was that
        one number governs both callers; a predicate that restates it agrees
        today and drifts silently the moment anyone edits the constant — which
        is precisely the edit the constant exists to make safe.

        Moving the number must move the answer. Asserted in both directions so
        it cannot pass by a coincidence of the fixture's size.
        """
        residues = [("A", i, " ") for i in range(1, 61)]
        segments = [("A", 1, 25)]           # 25 distinct residues
        assert rp.n_selected_residues(residues, segments) == 25

        monkeypatch.setattr(rp, "MIN_SELECTED_RESIDUES", 30)
        assert rp.target_too_small(residues, segments), (
            "raising the floor above the selection must refuse it; the "
            "predicate is not reading MIN_SELECTED_RESIDUES")

        monkeypatch.setattr(rp, "MIN_SELECTED_RESIDUES", 10)
        assert not rp.target_too_small(residues, segments), (
            "lowering the floor below the selection must accept it; the "
            "predicate is not reading MIN_SELECTED_RESIDUES")

    def _prepare(self, tmp_path, monkeypatch, target_input, spans=None):
        """Run ``prepare_custom_target`` against a 60-residue chain A.

        THE STRUCTURAL TESTS BELOW ARE NOT ENOUGH ON THEIR OWN, which is the
        lesson this branch keeps paying for: an AST check sees that a call
        exists, and a refusal that computes its verdict and never acts on it
        satisfies that exactly. So the floor is also EXECUTED, and what is
        asserted is the consequence — the process exits, and nothing is staged
        for the design engine.

        Everything outside ``tmp_path`` is patched away. Nothing here reaches
        ``complexa``: the registry read is the next step after the crop and it
        raises on a path that does not exist, which is a DIFFERENT ``check``
        name and is exactly what makes the at-the-floor control meaningful.
        """
        hub = tmp_path / "hub"
        results = tmp_path / "smoke_results.json"
        monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(hub))
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(results))
        monkeypatch.setattr(rp, "_TARGETS_DICT", str(tmp_path / "no_registry.yaml"))
        monkeypatch.setattr(
            rp, "download_target",
            lambda url, dest: dest.write_text(_make_pdb(spans or {"A": (1, 60)})))
        with pytest.raises(SystemExit) as excinfo:
            rp.prepare_custom_target(
                input_url="https://example.invalid/target.pdb", job_id="j1",
                target_chain="A", target_input=target_input, hotspot_spec=[],
                binder_length=[60, 120], run_dir=tmp_path / "run")
        assert excinfo.value.code == 1
        payload = json.loads(results.read_text())
        return payload["error"], sorted(p.name for p in hub.glob("hub_*.pdb"))

    @staticmethod
    def _size_fields(detail):
        """Production's size refusal, BY ROLE.

        ``str(floor) in detail`` is not a test of this sentence: transposing the
        count with the floor leaves both numbers in it, and the count can be
        supplied by the CONTIG rather than by the selection. Both numbers are
        parsed out of their slots instead.
        """
        match = re.search(
            r"has only (?P<count>\d+) residues, fewer than the (?P<floor>\d+) "
            r"needed", detail)
        assert match, f"the size refusal no longer renders its fields: {detail}"
        return int(match.group("count")), int(match.group("floor"))

    def test_a_contig_under_the_floor_is_refused_and_nothing_is_staged(
            self, tmp_path, monkeypatch):
        floor = rp.MIN_SELECTED_RESIDUES
        error, staged = self._prepare(
            tmp_path, monkeypatch, f"A1-{floor - 1}")
        assert error["check"] == "target_input", error
        count, quoted = self._size_fields(error["detail"])
        assert (count, quoted) == (floor - 1, floor), (
            f"the operator needs both the count and the floor: {error['detail']}")
        assert count < quoted, (
            f"the refusal quotes a floor below its own count: {error['detail']}")
        assert "Widen the chain range" in error["detail"]
        assert staged == [], (
            "the target was staged for the design engine despite the refusal")

    def test_an_overlapping_contig_is_refused_and_nothing_is_staged(
            self, tmp_path, monkeypatch):
        """THE MONEY DEFECT, END TO END THROUGH PRODUCTION.

        ``A1-19,A1-19`` is 19 residues written twice. The gate counted 38 and
        staged the target; the crop then wrote the 19 the gate had just decided
        were too few. On the web route the adapter happens to shield this — it
        refuses two OVERLAPPING ranges on one chain, and two identical ranges
        overlap — but ``prepare_custom_target`` is also reached with a contig
        the adapter never saw, and the canary bypasses the adapter entirely.

        The adapter's rule USED to be the broader "a chain may appear only
        once", which also refused the disjoint ``A1-50,A60-240`` a gapped
        target needs. This count is what made narrowing it safe: the floor is
        held here, on a de-duplicated key set, not by the form.
        """
        floor = rp.MIN_SELECTED_RESIDUES
        error, staged = self._prepare(
            tmp_path, monkeypatch, f"A1-{floor - 1},A1-{floor - 1}")
        assert error["check"] == "target_input", error
        count, quoted = self._size_fields(error["detail"])
        assert (count, quoted) == (floor - 1, floor), (
            f"the count in the message must be the DISTINCT one: {error['detail']}")
        assert staged == []

    def test_a_contig_at_the_floor_gets_past_the_gate(
            self, tmp_path, monkeypatch):
        """The control, and the reason the bound is ``<`` rather than ``<=``.

        It still fails — the registry does not exist here — but on a different
        check, and the staged file proves it reached the crop, which is
        downstream of the floor.
        """
        error, staged = self._prepare(
            tmp_path, monkeypatch, f"A1-{rp.MIN_SELECTED_RESIDUES}")
        assert error["check"] == "target_registry", error
        assert len(staged) == 1, (
            f"a target at the floor never reached the crop: {error}")

    @staticmethod
    def _prepare_ast():
        source = Path(rp.__file__).read_text(encoding="utf-8")
        return next(
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.FunctionDef) and n.name == "prepare_custom_target")

    @classmethod
    def _size_guard_ast(cls):
        return next(
            node for node in ast.walk(cls._prepare_ast())
            if isinstance(node, ast.If)
            and "target_too_small" in ast.unparse(node.test))

    def test_production_asks_the_predicate_instead_of_restating_the_number(self):
        """THE POINT OF EXTRACTING IT. ``prepare_custom_target`` used to hold
        ``if len(selected) < 20``. A second copy of a threshold is a threshold
        that drifts, and the canary — which now reads this one — would have gone
        on spending money against whichever copy it did not follow.

        SCOPED TO THE GUARD, NOT TO THE FUNCTION, and the narrowing is a fix.
        Scanning every literal in ``prepare_custom_target`` made the test fire
        on constants that have nothing to do with the floor: an unrelated
        ``ambiguous[:10]`` in the insertion-code warning meant a floor of 10
        "failed the drift test", and any future literal equal to the floor would
        do the same. What is being pinned is that the threshold reaches the
        refusal from the constant, which is a property of the guard.
        """
        called = {node.func.id for node in ast.walk(self._prepare_ast())
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert "target_too_small" in called, (
            "prepare_custom_target must ASK for the floor, not restate it")
        guard = self._size_guard_ast()
        literals = {node.value for node in ast.walk(guard)
                    if isinstance(node, ast.Constant) and isinstance(node.value, int)
                    and not isinstance(node.value, bool)}
        assert not literals, (
            f"the size guard carries its own numbers ({sorted(literals)}); the "
            "threshold must come from MIN_SELECTED_RESIDUES")

    def test_the_refusal_message_quotes_the_threshold(self):
        """The operator's next action is "widen the range to at least N", so N
        has to be in the sentence — and has to be the constant, or the message
        sends them to a number the code no longer enforces."""
        rendered = ast.unparse(self._size_guard_ast())
        assert "MIN_SELECTED_RESIDUES" in rendered, (
            f"the refusal must quote the threshold it enforces: {rendered}")

    def test_the_size_guard_runs_after_the_segment_and_numbering_ones(self):
        """PRODUCTION'S ORDER, PINNED ON PRODUCTION'S SIDE.

        The canary's ordering had a test; production's had none, so the guard
        could be moved above the per-segment check or the numbering one with the
        suite still green — and the canary, which mirrors production's order
        deliberately, would then be mirroring an order production no longer had.
        Statement positions rather than behaviour, because two of the three
        orderings are only observable on inputs that are invalid twice over.
        """
        body = self._prepare_ast().body
        def index_of(needle):
            return next(i for i, stmt in enumerate(body)
                        if needle in ast.unparse(stmt))
        size = index_of("target_too_small")
        assert index_of("unrenderable_segments") < size, (
            "a tagged construct must be told about its numbering, not its size")
        assert index_of("empty_segments") < size, (
            "a chain that is not in the file must not be told to widen a range")
        assert size < index_of("missing_hotspots"), (
            "a sliver puts most hotspots outside the selection; answering the "
            "hotspot sends the operator to fix one that is fine")

    def test_the_size_guard_beats_the_hotspot_one_behaviourally(
            self, tmp_path, monkeypatch):
        """The same ordering where it is observable. ``A41-59`` is a sliver AND
        puts ``A5`` outside the selection; the answer must be the range."""
        floor = rp.MIN_SELECTED_RESIDUES
        hub = tmp_path / "hub"
        results = tmp_path / "smoke_results.json"
        monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(hub))
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(results))
        monkeypatch.setattr(rp, "_TARGETS_DICT", str(tmp_path / "no_registry.yaml"))
        monkeypatch.setattr(
            rp, "download_target",
            lambda url, dest: dest.write_text(_make_pdb({"A": (1, 60)})))
        with pytest.raises(SystemExit):
            rp.prepare_custom_target(
                input_url="https://example.invalid/target.pdb", job_id="j1",
                target_chain="A", target_input=f"A41-{41 + floor - 2}",
                hotspot_spec=["A5"], binder_length=[60, 120],
                run_dir=tmp_path / "run")
        error = json.loads(results.read_text())["error"]
        assert error["check"] == "target_input", error
        assert self._size_fields(error["detail"]) == (floor - 1, floor)
        assert "A5" not in error["detail"], (
            f"the hotspot refusal won and misdirects the fix: {error['detail']}")

    def test_a_dead_segment_beats_the_size_guard(self, tmp_path, monkeypatch):
        """``A1-5,Z1-50`` is a sliver AND names a chain that is not there. The
        fix for the second is not "widen the range"."""
        error, staged = self._prepare(tmp_path, monkeypatch, "A1-5,Z1-50")
        assert error["check"] == "target_input", error
        assert "select 0 residues" in error["detail"], error
        assert "fewer than" not in error["detail"], (
            f"the size refusal answered a question about chain Z: {error}")
        assert staged == []

    def test_negative_numbering_beats_the_size_guard(self, tmp_path, monkeypatch):
        """``A-5-0`` on a file numbered from 1 selects nothing AND cannot be
        rendered. Upstream's parser is the fault the operator has to fix."""
        error, staged = self._prepare(tmp_path, monkeypatch, "A-5-0")
        assert error["check"] == "target_input_negative", error
        assert staged == []

    def test_a_two_chain_target_at_the_floor_reaches_the_crop(
            self, tmp_path, monkeypatch):
        """END TO END, ON THE MULTI-CHAIN SHAPE #109 ENABLED. A count that is
        per-first-chain or chain-blind refuses this, and every other behavioural
        fixture in this class is single-chain."""
        hi = rp.MIN_SELECTED_RESIDUES - 1
        error, staged = self._prepare(
            tmp_path, monkeypatch, f"A1-{hi},B1-{hi}",
            spans={"A": (1, hi), "B": (1, hi)})
        assert error["check"] == "target_registry", error
        assert len(staged) == 1, (
            f"a legitimate two-chain target never reached the crop: {error}")

    def test_the_floor_does_not_collide_with_the_shared_preflight_one(self):
        """TWO NUMBERS, TWO NAMES, ON PURPOSE.

        ``shared/pdb_preflight.py`` also exports a ``MIN_TARGET_RESIDUES``, and
        it is a DIFFERENT quantity with a different value: the lowest
        ``min_target_aa`` across the binder tools, bounding the whole named
        chain before any contig, on the ``/tools/<slug>/submit`` route. This one
        bounds the contig's SELECTION inside the container. They were briefly
        the same identifier with different values, in a commit whose thesis was
        "one number, one home".
        """
        from shared import pdb_preflight as pre
        assert not hasattr(rp, "MIN_TARGET_RESIDUES"), (
            "the proteina-local floor must not reuse shared's name")
        assert pre.MIN_TARGET_RESIDUES != rp.MIN_SELECTED_RESIDUES, (
            "if these ever coincide, say so deliberately — they measure "
            "different things and nothing keeps them equal")


class TestEmptyContigSegments:
    """A segment that selects nothing is a refusal, one segment at a time.

    ``prepare_custom_target`` has always checked per segment. What is new is
    that the check is a named predicate (``empty_segments``) the canary calls
    too: it checked only the AGGREGATE, so ``--contig A1-300,Z1-50`` selected
    300 residues, cleared every gate, and spawned ~$4 or ~$12 to fail in the
    container on a request production settles for free. PR #109 made
    multi-segment contigs the ordinary input shape.
    """

    _RESIDUES = [("A", i, "") for i in range(1, 61)] + [("B", i, "") for i in range(1, 41)]

    def test_a_dead_segment_is_found_beside_a_healthy_one(self):
        assert rp.empty_segments(self._RESIDUES, [("A", 1, 60), ("Z", 1, 50)]) == [
            ("Z", 1, 50)]
        assert rp.empty_segments(self._RESIDUES, [("A", 1, 60), ("A", 900, 999)]) == [
            ("A", 900, 999)]

    def test_healthy_segments_are_left_alone(self):
        assert rp.empty_segments(self._RESIDUES, [("A", 1, 60), ("B", 1, 40)]) == []
        assert rp.empty_segments(self._RESIDUES, [("A", 55, 900)]) == []

    def test_an_unresolvable_bare_chain_selects_nothing(self):
        """``expand_bare_chains`` leaves a chain it cannot find alone, and this
        is what then names it. Widening a range cannot add a missing chain, so
        the two cases share one refusal rather than two messages."""
        segments = rp.expand_bare_chains(self._RESIDUES, [("Z", None, None)])
        assert segments == [("Z", None, None)]
        assert rp.empty_segments(self._RESIDUES, segments) == [("Z", None, None)]

    def test_production_asks_the_predicate_instead_of_looping_inline(self):
        source = Path(rp.__file__).read_text(encoding="utf-8")
        prepare = next(
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.FunctionDef) and n.name == "prepare_custom_target")
        called = {node.func.id for node in ast.walk(prepare)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert {"empty_segments", "expand_bare_chains"} <= called, (
            "prepare_custom_target must ASK for these, not restate them — the "
            "canary reads the same two")

    def test_a_dead_segment_is_refused_and_nothing_is_staged(
            self, tmp_path, monkeypatch):
        """Behaviourally, through ``prepare_custom_target``: the aggregate is
        healthy (60 residues of chain A) and the run still stops for free."""
        error, staged = TestMinimumTargetSize()._prepare(
            tmp_path, monkeypatch, "A1-60,Z1-50")
        assert error["check"] == "target_input", error
        assert "Z" in error["detail"] and "0 residues" in error["detail"]
        assert staged == []

    def test_a_bare_chain_that_is_absent_is_named_not_rendered_as_None(
            self, tmp_path, monkeypatch):
        """PRODUCTION'S ABSENT-BARE-CHAIN REFUSAL, WHICH HAD NO TEST AT ALL.

        Found by an independent QC pass: deleting this refusal outright, and
        narrowing it to a single hard-coded chain, BOTH left the whole suite
        green. The run still stops either way — ``empty_segments`` catches it a
        few lines later — so no money is at stake, which is exactly why nothing
        noticed. What the operator sees is not the same:

            with the guard:    chain Q is not present in the uploaded target.
            without it:        chain Q residues None-None select 0 residues ...

        ``None-None`` is not a range anyone typed, and this refusal is the only
        thing standing between a customer and that string.

        It matters beyond the wording because three of this branch's own claims
        rest on it behaving as described — ``expand_bare_chains``' docstring
        says both callers already have a refusal for an absent bare chain and
        names this one, and the canary's "six of production's eight" count
        includes it. This commit restructured the refusal (out of the expansion
        loop into a standalone one) and added no coverage for it.

        THAT DENOMINATOR HAS SINCE MOVED, and is recorded here rather than
        quietly left to rot. Production grew a NINTH pre-GPU refusal — the
        contig run-count ceiling, ``run_pipeline.MAX_CONTIG_RUNS`` /
        ``target_input_runs`` — and ``_hotspot_canary`` does not mirror it, nor
        does it apply ``contig_runs`` to the contig it ships to
        ``build_target_add_cmd`` (``_hotspot_canary.py`` ``_stage``, and
        ``_refuse_unresolvable_hotspots``' ``resolved``). So on a target with a
        disordered loop the canary still spawns and dies where production now
        refuses for free. That is the safe direction for correctness and the
        expensive one for the operator (~$4 phase 1, ~$12 phase 2), and it is
        the drift this file's own history keeps finding. Fix it in the canary
        before the next paid phase.
        """
        error, staged = TestMinimumTargetSize()._prepare(
            tmp_path, monkeypatch, "Q")
        assert error["check"] == "target_input", error
        assert "chain Q is not present" in error["detail"], error["detail"]
        assert "None" not in error["detail"], (
            "the absent-chain refusal is gone and the unexpanded segment is "
            f"leaking into the message: {error['detail']}")
        assert staged == []


class TestBareChainExpansion:
    """``--contig A`` means "the whole chain", and it must be RESOLVED, not
    dropped.

    Production expands a bare chain id to the chain's observed span and only
    then applies the numeric guards, so ``A`` on a construct numbered from -5
    becomes ``A-5-240`` and is refused for negative numbering. The canary had
    no expansion and FILTERED unexpanded segments out of that guard instead, so
    the same input spawned. The expansion is extracted here so both sides run
    the one implementation — and so neither is tempted to "fix" it by refusing
    the bare id, which would refuse a run production accepts.
    """

    _RESIDUES = [("A", i, "") for i in range(-5, 25)] + [("B", i, "") for i in range(1, 41)]

    def test_a_bare_chain_becomes_its_observed_span(self):
        assert rp.expand_bare_chains(self._RESIDUES, [("A", None, None)]) == [
            ("A", -5, 24)]
        assert rp.expand_bare_chains(self._RESIDUES, [("B", None, None)]) == [
            ("B", 1, 40)]

    def test_an_explicit_range_is_untouched(self):
        assert rp.expand_bare_chains(self._RESIDUES, [("A", 1, 20)]) == [("A", 1, 20)]
        assert rp.expand_bare_chains(
            self._RESIDUES, [("A", None, None), ("B", 2, 9)]) == [
                ("A", -5, 24), ("B", 2, 9)]

    def test_the_expansion_feeds_the_negative_numbering_guard(self):
        """The composition that was missing. A bare chain id on a tagged
        construct is unrenderable ONCE EXPANDED and invisible before that."""
        segments = rp.expand_bare_chains(self._RESIDUES, [("A", None, None)])
        assert rp.unrenderable_segments(segments) == [("A", -5, 24)]

    def test_unrenderable_segments_tolerates_an_unresolved_chain(self):
        """It is asked about parsed contigs now, and a parsed contig may carry
        a chain that is not in the file. That is not a NEGATIVE NUMBER — it is
        an empty segment, refused with a different message and a different fix
        — so it must not be reported here, and must not raise."""
        assert rp.unrenderable_segments([("Z", None, None)]) == []
        assert rp.unrenderable_segments(
            [("Z", None, None), ("A", -5, 24)]) == [("A", -5, 24)]

    def test_a_bare_chain_on_a_tagged_construct_is_refused_by_production(
            self, tmp_path, monkeypatch):
        """The behavioural half, and the reason the canary must EXPAND rather
        than refuse: production accepts the bare id and refuses the span."""
        hub = tmp_path / "hub"
        results = tmp_path / "smoke_results.json"
        monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(hub))
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(results))
        monkeypatch.setattr(rp, "_TARGETS_DICT", str(tmp_path / "no_registry.yaml"))
        monkeypatch.setattr(
            rp, "download_target",
            lambda url, dest: dest.write_text(_make_pdb({"A": (-5, 40)})))
        with pytest.raises(SystemExit):
            rp.prepare_custom_target(
                input_url="https://example.invalid/target.pdb", job_id="j1",
                target_chain="A", target_input="A", hotspot_spec=[],
                binder_length=[60, 120], run_dir=tmp_path / "run")
        error = json.loads(results.read_text())["error"]
        assert error["check"] == "target_input_negative", error
        assert "A-5-40" in error["detail"]


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


def _render_results(candidates):
    """The real proteina results partial, rendered over ``candidates``.

    ``results_panel`` is stubbed: these tests are about the banner the partial
    adds, not about the shared shell (which needs url_for, csrf_input and the
    metric glossary to render at all).

    MODULE LEVEL, not a method, because ``TestUploadLoopNumbering`` renders the
    candidates a real ``main()`` run produced through it. That composition is
    the only thing that can catch a banner which is false for a whole class of
    runs: a template test alone asserts whatever value the test itself passes
    in, and a pipeline test alone never looks at the words the operator reads.
    """
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
    templates = Path(__file__).resolve().parents[1] / "templates"
    env = Environment(loader=ChoiceLoader([
        # ``caller()`` has to be referenced or Jinja refuses the {% call %}
        # with "two values for the special caller argument".
        DictLoader({"components/results_shell.html": (
            "{% macro results_panel(candidates, columns, tool_slug, job_id,"
            " clone_url='', tier='', gpu_seconds=None,"
            " send_target_tools=None, campaign_id='') %}"
            "PANEL{% if not candidates %}{{ caller() }}{% endif %}"
            "{% endmacro %}")}),
        FileSystemLoader(str(templates)),
    ]))
    return env.get_template("tools/proteina_results.html").render(
        job=SimpleNamespace(result={"candidates": candidates}, id="j1"),
        send_target_tools=[])


class TestResultsTemplateSurfacesTheNumbering:
    """B6. ``target_numbering`` was written into the result and rendered nowhere.

    It went into ``out_designs`` only, while shared/jobs.py::candidate_records
    prefers ``candidates`` and this partial reads ``candidates``, so the operator
    had no way at all to learn whether the file they downloaded is keyed to the
    numbers they typed. A field nobody can see is not a record of anything.
    """

    def _render(self, candidates):
        return _render_results(candidates)

    def _cand(self, rank, **kw):
        return dict({"rank": rank, "pdb_key": f"design_{rank}.pdb",
                     "scores": {}}, **kw)

    def test_it_says_so_when_the_operators_numbering_was_restored(self):
        html = self._render([self._cand(0, target_numbering="input"),
                             self._cand(1, target_numbering="input")])
        assert "Residue numbering" in html
        assert "residue numbers from the file you uploaded" in html

    def test_it_says_so_when_the_file_carries_upstreams_numbering(self):
        html = self._render([self._cand(0, target_numbering="upstream")])
        assert "Residue numbering" in html
        assert "renumbered from 1" in html
        assert "will not resolve" in html

    def test_a_mixed_shard_reports_the_weaker_guarantee(self):
        """One design in 1..N is enough to make "your hotspots resolve" false
        for the download as a whole."""
        html = self._render([self._cand(0, target_numbering="input"),
                             self._cand(1, target_numbering="upstream")])
        assert "renumbered from 1" in html
        assert "residue numbers from the file you uploaded" not in html

    def test_a_result_from_before_the_field_existed_claims_nothing(self):
        """Jinja's ``map(attribute=...)`` yields Undefined -- not None -- for a
        missing key, so the obvious ``| reject('none')`` spelling would let old
        candidates through and print the reassuring message about a file nobody
        checked. Silence is the only honest output here."""
        html = self._render([self._cand(0), self._cand(1)])
        assert "Residue numbering" not in html

    def test_no_candidates_at_all_claims_nothing(self):
        assert "Residue numbering" not in self._render([])

    def test_a_null_candidate_list_renders_instead_of_raising(self):
        """F10. ``output.get('candidates', [])`` returns the DEFAULT only when
        the key is absent; a stored ``"candidates": null`` returns None, and the
        counting loop this delta added sits OUTSIDE the ``{% if candidates %}``
        the old panel was guarded by. So a null there stopped rendering the
        whole results page with ``TypeError: 'NoneType' object is not
        iterable`` where it used to render fine.

        Unreachable from either writer today — both emit a list — so this is a
        robustness regression rather than a live defect, which is why it is
        pinned rather than merely fixed.
        """
        assert "Residue numbering" not in self._render(None)


def test_sanitize_candidate_keeps_the_target_numbering():
    """The streamed half of the same path. ``_sanitize_candidate`` drops every
    key it does not name, so the live candidate the status page renders would
    have lost this field even once the pipeline sent it."""
    from webhooks.modal import _sanitize_candidate

    out = _sanitize_candidate({"rank": 0, "pdb_key": "design_000.pdb",
                               "target_numbering": "input"})
    assert out is not None
    assert out["target_numbering"] == "input"
    assert _sanitize_candidate(
        {"rank": 0, "target_numbering": "upstream"})["target_numbering"] == "upstream"


def test_sanitize_candidate_rejects_an_unrecognised_numbering():
    """The heartbeat body is unauthenticated telemetry that gets rendered back
    to the user, so every string field here is bounded. This one is bounded to
    the values the pipeline can emit rather than by a length cap."""
    from webhooks.modal import _sanitize_candidate

    for bogus in ("<script>", "INPUT", "", 7, None, "curated", "n/a ", "N/A"):
        out = _sanitize_candidate({"rank": 0, "target_numbering": bogus})
        assert out["target_numbering"] is None, bogus


# The three answers to "which residue numbering does the delivered file
# carry?", written out HERE rather than read off the pipeline, so that deleting
# one from either side fails this file rather than silently shrinking it.
_TARGET_NUMBERING_VALUES = ("input", "upstream", "n/a")


def test_the_webhook_allowlist_covers_every_numbering_the_pipeline_emits():
    """F1's other half. ``_sanitize_candidate`` drops any value it does not
    name, and the STREAMED candidate is what the live status page renders. A
    third value added to the pipeline and not to the allowlist would make the
    same design report one numbering while it is running and another once it
    finalised — the drift is invisible until an operator compares the two.

    Both directions are pinned: the pipeline may not emit a value the webhook
    would drop, and the webhook may not silently accept one the pipeline never
    emits (this endpoint's body is unauthenticated).

    G2: THE REVERSE HALF WAS A CLAIM IN THIS DOCSTRING AND NOTHING IN THE BODY.
    Widening the webhook's tuple to ``("input", "upstream", "n/a", "foo")``
    passed all 384 tests in this file — it was the only mutation of thirty to
    survive. ``test_sanitize_candidate_rejects_an_unrecognised_numbering``
    looks like the missing half and is not: it catches ``"curated"`` only
    because that string is in its own hard-coded list, and a hard-coded list
    cannot contain the value someone will add next.

    So the allowlist is READ rather than probed. A probe pins the property in
    the readable direction; only reading the gate itself can fail on a value
    nobody thought to write down.
    """
    from webhooks.modal import _sanitize_candidate

    assert tuple(rp._TARGET_NUMBERING_VALUES) == _TARGET_NUMBERING_VALUES
    for value in _TARGET_NUMBERING_VALUES:
        got = _sanitize_candidate({"rank": 0, "target_numbering": value})
        assert got["target_numbering"] == value, value

    # The gate is ``raw_numbering in (...)`` and the compiler folds that tuple
    # into a constant of the function, so this is the ACTUAL allowlist rather
    # than a copy of it. If the gate ever stops being an inline literal this
    # assertion fails loudly and says where to look, which is the right
    # outcome: a test that cannot find the allowlist must not report on it.
    gate = [c for c in _sanitize_candidate.__code__.co_consts
            if isinstance(c, (tuple, frozenset)) and "input" in c]
    assert len(gate) == 1, (
        "the numbering allowlist is no longer a single inline tuple inside "
        f"_sanitize_candidate (found {gate!r}) — point this test at wherever "
        "it now lives rather than deleting it")
    assert set(gate[0]) == set(rp._TARGET_NUMBERING_VALUES), (
        f"the webhook accepts {sorted(set(gate[0]))} but the pipeline can only "
        f"emit {sorted(set(rp._TARGET_NUMBERING_VALUES))}")

    # ...and the same statement behaviourally, so the failure above is not the
    # only thing standing between this endpoint and an echoed string.
    probe = "not-a-numbering"
    assert probe not in rp._TARGET_NUMBERING_VALUES
    assert _sanitize_candidate(
        {"rank": 0, "target_numbering": probe})["target_numbering"] is None



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

    IT HAS BEEN WRONG TWICE. It shipped claiming "30 to 120" minutes per shard
    for all three design variants — a placeholder 5-20x above anything real.
    It was then corrected to "~6" from a 359 s reading, which was also wrong:
    two readings exist at 130 aa and they disagree by ~60% — 359 s and 576 s.
    Both are recorded as completed 8-design shards at that size, and what
    separates them is the JAX allocator regime: 359 s is the preallocation-ON
    shard, 576 s is one of the three taken with preallocation off.
    shared/pdb_preflight_rules.py::_PROTEINA documents those regimes as
    non-comparable, so 576 s is the reading that describes what production
    does today and copy drawn from the 359 s one under-stated a real shard by
    ~40%. The older figure is simply not used for anything.

    Three COMPLETED shards now exist — 576 s at 130 aa, 645 s at 260 aa, 874 s
    at 415 aa, i.e. 9.6 to 14.6 min. Both errors were load-bearing beyond the
    copy: shared/pdb_preflight_rules.py anchors its runtime estimator here, so
    a wrong number in a docs constant reaches the preflight panel looking
    calibrated.
    """

    def test_protein_binder_runtime_reflects_the_completed_shards(self):
        from tools.proteina import meta
        entry = str(meta.PRESET_RUNTIME["protein_binder"]["typical_minutes"])
        assert "30 to 120" not in entry, (
            "protein_binder still quotes the placeholder band; the completed "
            "shards run 9.6 to 14.6 min across 130-415 residues")
        assert entry.strip() != "~6", (
            "protein_binder still quotes the 359 s reading; that one was "
            "taken with JAX preallocation ON and is not comparable to the "
            "three this band is drawn from, where the 130 aa shard took 576 s")
        # THE BAND MUST BRACKET THE MEASUREMENT, and that has to be read as
        # numbers rather than as substrings. ``"10" in entry and "15" in
        # entry`` was satisfied by "~10 to 150" — a ceiling ten times anything
        # ever run — and by any string carrying those two digit pairs
        # anywhere. It also passed the band it was written for, "~10 to 15",
        # whose FLOOR sat above the 9.6 min shard it claimed to describe.
        # BOTH ENDS ARE BOUNDED FROM BOTH SIDES. A one-sided bound on either
        # end lets the band be widened into meaninglessness in the direction
        # it is not watched, and this copy's whole history is being wrong in
        # the direction the user plans against. With only `lo <= 9.6` and
        # `hi >= 14.6` in force, "~5 to 15", "~1 to 15" and "~0.1 to 15" all
        # passed the entire suite; with `hi <= 20.0`, so did "~9 to 19".
        # Each end must be a ROUNDING of the shard it describes.
        bounds = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", entry)]
        assert len(bounds) == 2, f"{entry!r} is not a two-ended band"
        lo, hi = bounds
        assert lo <= 9.6, (
            f"{entry!r} claims a floor above the fastest completed shard "
            f"(576 s = 9.6 min at 130 aa)")
        assert lo >= 9.0, (
            f"{entry!r} claims a floor below anything this tool has done: the "
            f"fastest completed shard is 9.6 min, and 9 is that rounded down "
            f"to the whole minute — a floor under it is not a rounding of the "
            f"measurement, it is a different claim")
        assert hi >= 14.6, (
            f"{entry!r} claims a ceiling below the slowest completed shard "
            f"(874 s = 14.6 min at 415 aa)")
        assert hi <= 16.0, (
            f"{entry!r} claims a ceiling above anything measured OR modelled: "
            f"the largest target ever run took 14.6 min and the 500-aa cap "
            f"models at ~14.6 too, so 15 is the whole-minute rounding and 16 "
            f"is already a minute of slack past it")

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
        # And the two copies must quote the SAME band, not merely both be
        # non-placeholder — the whole failure mode here is one of them moving.
        #
        # COMPARED AS NUMBERS, NOT AS A SUBSTRING. `band in row` was the
        # obvious way to write this and it does not do the job: the about
        # table reads "<band> min / shard (measured at 130-415 residues)", so
        # the shipped band is a PREFIX of it, and every string that extends
        # the shipped band's own digits contains it too. "~9 to 15" is in
        # "~9 to 15000 min / shard", so moving only the about-table copy to a
        # ceiling a thousand times anything ever run left this test green —
        # which is precisely the drift the assertion names.
        band = str(meta.PRESET_RUNTIME["protein_binder"]["typical_minutes"])
        row = rows["protein_binder"]
        band_nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", band)]
        # The about row carries the measured span (130-415) after the band, so
        # take the leading pair — the band is what this compares.
        row_nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", row)][:2]
        assert len(band_nums) == 2 and row_nums == band_nums, (
            f"about.runtime_table quotes {row!r} while PRESET_RUNTIME quotes "
            f"{band!r} — parsed as {row_nums} against {band_nums}; the tool "
            f"page and the preset map are telling the user different things")

    def test_the_estimator_anchor_is_no_longer_taken_from_this_file(self):
        """The specific coupling that turned a docs placeholder into a number
        the preflight panel presented as calibrated.

        Both retired anchors are named, because "not 75" alone would be
        satisfied by the 5.4 that came from the 359 s reading.
        """
        from shared.pdb_preflight_rules import TOOL_RULES
        env = TOOL_RULES["proteina"].size
        base = env.runtime_base_min
        assert base != 75.0, (
            "runtime_base_min is still the midpoint of meta.py's retired "
            "30-120 min band")
        assert base != 5.4, (
            "runtime_base_min is still solved from the 359 s reading — the "
            "one of the two 130 aa readings taken with JAX preallocation ON, "
            "which _PROTEINA documents as not comparable to the three "
            "preallocation-off shards the shipped curve is fitted to")
        # base x (aa/120)^alpha x (8/8) must reproduce all three completed
        # shards, not just one — a single-point check cannot see the exponent.
        for aa, secs in ((130, 576), (260, 645), (415, 874)):
            est = base * (aa / 120.0) ** env.runtime_alpha
            measured = secs / 60.0
            assert abs(est - measured) / measured <= 0.10, (
                f"the estimator puts the {aa} aa 8-design shard at "
                f"{est:.1f} min, not the {measured:.1f} min it actually took")


# ---------------------------------------------------------------------------
# Putting the DELIVERED design back into the operator's residue numbering
# ---------------------------------------------------------------------------
#
# Measured on a completed Fc shard: upstream renumbers every design chain to
# 1..N, so an operator who asked for hotspot A241 gets back a file with no
# residue 241 in it. Against the 8 archived designs, restore_design_numbering
# takes hotspot resolution from 0/20 to 20/20 while leaving coordinates, residue
# names and the binder chain byte-identical.

# 20 DISTINCT residue names, so a positional map is actually CONSTRAINED by
# sequence. An all-ALA fixture would score identity 1.0 against any reference of
# the same length and would not exercise the guard at all.
_SEQ_A = ["ALA", "GLY", "SER", "THR", "VAL", "LEU", "ILE", "PRO", "PHE", "TYR",
          "TRP", "HIS", "LYS", "ARG", "ASP", "GLU", "ASN", "GLN", "MET", "CYS"]
_SEQ_B = ["CYS", "MET", "GLN", "ASN", "GLU", "ASP", "ARG", "LYS", "HIS", "TRP",
          "TYR", "PHE", "PRO", "ILE", "LEU", "VAL", "THR", "SER", "GLY", "ALA"]
_SEQ_C = ["GLY", "ALA", "SER", "VAL", "LEU", "THR", "PRO", "PHE"]


def _chain(chain, resnames, first_res, serial0=1, icodes=None):
    """CA-only lines for one chain, in the real column layout.

    ``icodes``, when given, is a per-residue insertion code and ``first_res`` is
    then the number every one of them shares. Kabat/Chothia numbering does
    exactly this, and it is the case the first version of the restore corrupted.
    """
    if icodes is not None:
        return [_atom(serial0 + i, "CA", nm, chain, first_res, icode=ic)
                for i, (nm, ic) in enumerate(zip(resnames, icodes))]
    return [_atom(serial0 + i, "CA", nm, chain, first_res + i)
            for i, nm in enumerate(resnames)]


def _with_altloc(line, code):
    """``line`` with the altLoc column (col 17, 0-indexed 16) set to ``code``.

    ``_atom`` writes a blank there. Two lines that differ ONLY in this column
    are the two conformations of one residue, not two residues.
    """
    return line[:16] + code + line[17:]


def _ter(serial, resname, chain, resseq):
    """A TER record padded to 80 columns, as upstream really writes them.

    MEASURED on the archived designs: every TER there is 80 characters. The
    fixture used to emit a 25-character TER, which is both unrealistic and
    unable to hold an insertion code — so a rewrite that silently GREW the line
    to fit one would have looked fine here. ``test_every_line_keeps_its_width``
    is what makes that matter, and it needs a fixture whose lines are the width
    a real file's are.
    """
    return ("TER   %5d      %3s %1s%4d " % (serial, resname, chain, resseq)).ljust(80)


def _design(*, a_first=1, b_first=1, c_first=1,
            seq_a=None, seq_b=None, seq_c=None, ter=True):
    """A renumbered design: target chains A and B, de-novo binder C."""
    seq_a = _SEQ_A if seq_a is None else seq_a
    seq_b = _SEQ_B if seq_b is None else seq_b
    seq_c = _SEQ_C if seq_c is None else seq_c
    lines = []
    lines += _chain("A", seq_a, a_first, 1)
    if ter:
        lines.append(_ter(900, seq_a[-1], "A", a_first + len(seq_a) - 1))
    lines += _chain("B", seq_b, b_first, 100)
    if ter:
        # Upstream writes a CUMULATIVE index here, not the chain's own number --
        # measured: a real design's chain B ends at ATOM 208 with TER 419.
        lines.append(_ter(901, seq_b[-1], "B", 999))
    lines += _chain("C", seq_c, c_first, 200)
    if ter:
        lines.append(_ter(902, seq_c[-1], "C", 888))
    return "\n".join(lines) + "\n"


def _ref_chain(resnames, first_res, icodes=None):
    """One reference chain as ``pdb_ca_sequence`` returns it.

    ``(resseq, icode, resname)`` — the icode is part of the residue id, not
    decoration, and dropping it here is exactly the defect that let three input
    residues collapse onto one number in the delivered file.
    """
    if icodes is not None:
        return [(first_res, ic, nm) for nm, ic in zip(resnames, icodes)]
    return [(first_res + i, "", nm) for i, nm in enumerate(resnames)]


def _reference(*, a_first=234, b_first=300, seq_a=None, seq_b=None):
    """What pdb_ca_sequence returns for the staged, cropped input target."""
    return {
        "A": _ref_chain(_SEQ_A if seq_a is None else seq_a, a_first),
        "B": _ref_chain(_SEQ_B if seq_b is None else seq_b, b_first),
    }


def _keys(text):
    """The ``A241``-style residue tokens an operator's hotspot list is made of."""
    return {"%s%d%s" % (c, r, i)
            for c, v in rp.pdb_ca_sequence(text).items() for r, i, _ in v}


# The five coordinate records WRITTEN OUT rather than read off
# ``rp._RESSEQ_COORD_RECORDS``. This helper is what checks the rewrite's
# headline promise, and a helper that shrinks whenever the code under test
# shrinks would stop looking at exactly the records that stopped being
# rewritten.
_COORD_RECORDS = ("ATOM  ", "HETATM", "ANISOU", "SIGATM", "SIGUIJ")


def _residues_in_file(text):
    """The distinct residues in ``text``, in file order.

    A residue is a MAXIMAL RUN of consecutive coordinate records sharing
    ``(chain, resSeq, iCode, resName)`` — how any PDB reader groups atoms into
    residues, and why ``ANISOU`` interleaved with its own ``ATOM`` is one
    residue rather than two.
    """
    runs, prev = [], None
    for line in str(text).split("\n"):
        if line[:6] not in _COORD_RECORDS:
            continue
        key = (line[21:22], line[22:26].strip(), line[26:27].strip(),
               line[17:20].strip())
        if key != prev:
            runs.append(key)
        prev = key
    return runs


def _duplicate_residue_ids(text):
    """``(chain, resSeq, iCode)`` ids carried by more than one residue.

    THE PROPERTY THE RESTORE PROMISES, stated over the delivered bytes instead
    of over a refusal message. A refusal test proves the code declines the
    inputs the test thought of; this proves the file that actually ships does
    not contain two different residues wearing one residue id.
    """
    ids = [k[:3] for k in _residues_in_file(text)]
    return sorted({i for i in ids if ids.count(i) > 1})


class TestPdbCaSequence:
    def test_it_agrees_with_pdb_ca_residues_about_what_a_residue_is(self, tmp_path):
        path = tmp_path / "f.pdb"
        path.write_text(FIXTURE_PDB, encoding="utf-8")
        by_chain = rp.pdb_ca_sequence(FIXTURE_PDB)
        flat = {(c, r, i) for c, v in by_chain.items() for r, i, _ in v}
        residues, _ = rp.pdb_ca_residues(path)
        assert flat == set(residues)

    def test_it_carries_the_residue_name(self):
        assert rp.pdb_ca_sequence(_design())["A"][0] == (1, "", "ALA")

    def test_it_carries_the_insertion_code(self):
        """THE BLOCKER, at its source. The de-dupe key already had the icode and
        the stored tuple threw it away one line later, so ``A100``, ``A100A``
        and ``A100B`` became three entries all keyed 100 -- three map values
        that collide, and a delivered file with three residues numbered 100."""
        text = "\n".join(_chain("A", ["TRP", "ALA", "GLY"], 100,
                                icodes=["", "A", "B"]))
        assert rp.pdb_ca_sequence(text)["A"] == [
            (100, "", "TRP"), (100, "A", "ALA"), (100, "B", "GLY")]

    def test_it_returns_each_chain_ascending(self):
        text = "\n".join(_chain("A", ["GLY", "ALA", "SER"], 5)[::-1])
        assert [r for r, _i, _n in rp.pdb_ca_sequence(text)["A"]] == [5, 6, 7]

    def test_ties_break_on_the_insertion_code_not_the_residue_name(self):
        """A ``(resseq, resname)`` sort broke ties on the NAME: file order
        TRP(100) / ALA(100A) / GLY(100B) parsed back as ALA / GLY / TRP, which
        scrambles the positional correspondence before anything can check it.
        The names here are chosen so the two orders differ."""
        text = "\n".join(_chain("A", ["TRP", "ALA", "GLY"], 100,
                                icodes=["", "A", "B"]))
        assert [n for _r, _i, n in rp.pdb_ca_sequence(text)["A"]] == [
            "TRP", "ALA", "GLY"]

    def test_a_blank_insertion_code_sorts_first(self):
        """PDB's own convention, and the fixture is written out of order so the
        sort has to do the work."""
        lines = _chain("A", ["ALA", "GLY", "SER"], 7, icodes=["B", "", "A"])
        assert [i for _r, i, _n in rp.pdb_ca_sequence("\n".join(lines))["A"]] == [
            "", "A", "B"]

    def test_it_stops_at_the_first_endmdl(self):
        text = _design() + "ENDMDL\n" + "\n".join(_chain("Z", ["ALA"] * 5, 1))
        assert "Z" not in rp.pdb_ca_sequence(text)

    def test_an_altloc_pair_is_one_residue_not_two(self):
        """F4. Deleting the ``if key in seen: continue`` de-dupe left the whole
        file green, and it is load-bearing: a side chain modelled in two
        conformations writes TWO ``CA`` lines for one residue, so without the
        de-dupe the chain comes back one residue longer than it is. Alternate
        conformations are ordinary in any crystal structure.
        """
        lines = _chain("A", ["ALA", "GLY", "SER"], 5)
        lines.insert(2, _with_altloc(lines[1], "B"))
        lines[1] = _with_altloc(lines[1], "A")
        got = rp.pdb_ca_sequence("\n".join(lines))["A"]
        assert [r for r, _i, _n in got] == [5, 6, 7]

    def test_an_altloc_pair_keeps_the_first_conformation_it_saw(self):
        """The de-dupe must KEEP one, not drop both, and it must be the first
        in file order — the same rule ``pdb_ca_residues`` follows, so the two
        parsers cannot disagree about which residue is at that id."""
        lines = _chain("A", ["ALA", "GLY", "SER"], 5)
        alt = _with_altloc(lines[1], "B")
        alt = alt[:17] + "TRP" + alt[20:]        # a DIFFERENT name, altloc B
        lines[1] = _with_altloc(lines[1], "A")
        lines.insert(2, alt)
        got = rp.pdb_ca_sequence("\n".join(lines))["A"]
        assert got == [(5, "", "ALA"), (6, "", "GLY"), (7, "", "SER")]


class TestRestoreDesignNumbering:
    def test_a_renumbered_design_is_put_back_into_the_input_numbering(self):
        out, rep = rp.restore_design_numbering(_design(), ["A", "B"], _reference())
        assert rep["applied"] is True
        got = rp.pdb_ca_sequence(out)
        assert [r for r, _i, _n in got["A"]] == list(range(234, 254))
        assert [r for r, _i, _n in got["B"]] == list(range(300, 320))

    def test_the_operators_hotspot_tokens_resolve_only_after_the_restore(self):
        design = _design()
        assert "A241" not in _keys(design)
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is True
        assert "A241" in _keys(out)
        assert "B305" in _keys(out)

    def test_the_binder_chain_is_never_renumbered(self):
        design = _design()
        out, _ = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rp.pdb_ca_sequence(out)["C"] == rp.pdb_ca_sequence(design)["C"]

    def test_a_chain_the_caller_did_not_name_is_left_alone_even_when_mappable(self):
        """F9. The test above does not discriminate its own property: chain C is
        absent from the reference, so ANY code that ignored ``target_chains``
        would refuse the whole file on C and leave C untouched by accident.
        "Untouched because nothing happened" and "untouched because it was not
        named" are different guarantees and only one of them is the binder's.

        Here the reference DOES carry a chain C that would map cleanly, so the
        only thing keeping it at 1..8 is that the caller did not name it. Code
        that renumbered every chain the reference knows about would move C to
        700..707 and fail this while still passing the test above.
        """
        ref = _reference()
        ref["C"] = _ref_chain(_SEQ_C, 700)
        design = _design()
        out, rep = rp.restore_design_numbering(design, ["A", "B"], ref)
        assert rep["applied"] is True, rep["reason"]
        assert [r for r, _i, _n in rp.pdb_ca_sequence(out)["A"]] == list(range(234, 254))
        assert rp.pdb_ca_sequence(out)["C"] == rp.pdb_ca_sequence(design)["C"]

    def test_alternate_conformations_do_not_defeat_the_length_check(self):
        """F4, end to end. A residue modelled in two conformations writes two
        ``CA`` lines; without ``pdb_ca_sequence``'s de-dupe the design chain
        parses one residue LONGER than the input, the length check refuses, and
        the operator silently receives 1..N — the exact outcome the restore
        exists to prevent, triggered by an ordinary crystal structure.

        Both conformation lines must also be rewritten: leaving one behind on
        upstream's number is a delivered file whose two conformers disagree
        about which residue they are.
        """
        lines = _chain("A", _SEQ_A, 1, 1)
        lines[4] = _with_altloc(lines[4], "A")
        lines.insert(5, _with_altloc(lines[4], "B"))
        design = ("\n".join(lines + _chain("B", _SEQ_B, 1, 100)
                            + _chain("C", _SEQ_C, 1, 200)) + "\n")
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is True, rep["reason"]
        assert [r for r, _i, _n in rp.pdb_ca_sequence(out)["A"]] == list(range(234, 254))
        at_238 = [l[16:17] for l in out.split("\n")
                  if l[:6] == "ATOM  " and l[21:22] == "A" and l[22:26] == " 238"]
        assert at_238 == ["A", "B"], "one conformation kept upstream's number"

    def test_coordinates_and_residue_names_are_untouched(self):
        design = _design()
        out, _ = rp.restore_design_numbering(design, ["A", "B"], _reference())
        coords = lambda t: [l[30:54] for l in t.split("\n") if l[:6] == "ATOM  "]
        names = lambda t: [l[17:20] for l in t.split("\n") if l[:6] == "ATOM  "]
        assert coords(out) == coords(design)
        assert names(out) == names(design)

    def test_only_the_resseq_and_icode_columns_change(self):
        design = _design()
        out, _ = rp.restore_design_numbering(design, ["A", "B"], _reference())
        differing = {i
                     for a, b in zip(design.split("\n"), out.split("\n"))
                     for i, (x, y) in enumerate(zip(a, b)) if x != y}
        assert differing <= {22, 23, 24, 25, 26}

    def test_every_line_keeps_its_width(self):
        """The companion the column test cannot do without. ``zip`` stops at the
        shorter of two lines, so a rewrite that GREW a line -- by splicing an
        insertion code into a record with no column for one, say -- would be
        invisible to the comparison above. PDB is fixed-column: a line that
        changes width has moved every field to its right."""
        design = _design()
        out, _ = rp.restore_design_numbering(design, ["A", "B"], _reference())
        before, after = design.split("\n"), out.split("\n")
        assert len(before) == len(after)
        assert [len(l) for l in before] == [len(l) for l in after]

    def test_the_line_count_is_preserved(self):
        design = _design()
        out, _ = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert len(out.split("\n")) == len(design.split("\n"))

    def test_ter_is_re_derived_from_the_chains_last_coordinate_record(self):
        # Chain A's TER AGREED with its atoms before the rewrite and must still
        # agree after -- mapping it would have been fine, but leaving it alone
        # would break something that was correct. Chain B's carried upstream's
        # cumulative 999, which is not a key in the map at all. The binder's is
        # left exactly as found.
        out, rep = rp.restore_design_numbering(_design(), ["A", "B"], _reference())
        assert rep["applied"] is True
        ters = {l[21:22]: int(l[22:26])
                for l in out.split("\n") if l.startswith("TER")}
        assert ters["A"] == 253
        assert ters["B"] == 319
        assert ters["C"] == 888

    def test_a_design_already_in_the_input_numbering_is_left_alone(self):
        design = _design(a_first=234, b_first=300)
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is False
        assert rep["already_input_numbering"] is True
        assert out == design

    def test_matching_numbers_alone_do_not_prove_the_numbering_is_the_inputs(self):
        """B3. ``already_input_numbering`` used to return BEFORE the sequence
        check, so identical residue NUMBERS were enough to claim the delivered
        file carries the operator's numbering.

        The case that breaks it is ordinary: a target numbered from 1 -- an
        AlphaFold model, say -- and a design where upstream emitted the BINDER
        as chain A. The key lists are then identical, ``[] == []``-style
        reasoning says "nothing to do", and the payload reports
        ``target_numbering: "input"`` for a chain the code itself scored at 0%
        identity. "Already correct" has to mean recognised, not merely
        same-shaped.
        """
        reference = _reference(a_first=1, b_first=1)
        design = _design(a_first=1, b_first=1, seq_a=list(reversed(_SEQ_A)))
        out, rep = rp.restore_design_numbering(design, ["A", "B"], reference)
        assert rep["already_input_numbering"] is False
        assert rep["applied"] is False
        assert out == design
        assert "chain A" in rep["reason"]
        assert "sequence identity" in rep["reason"]

    def test_a_design_that_really_is_already_correct_still_says_so(self):
        """The companion: gating "already" on the sequence check must not turn
        a genuinely already-correct design into a refusal."""
        reference = _reference(a_first=1, b_first=1)
        out, rep = rp.restore_design_numbering(_design(), ["A", "B"], reference)
        assert rep["already_input_numbering"] is True
        assert out == _design()

    def test_a_blank_chain_id_is_not_silently_dropped(self):
        """``{c for c in target_chains if c}`` dropped the blank chain id, which
        is a legal PDB chain and a legitimate key in the reference. The file was
        then rewritten with every OTHER chain renumbered and that one left in
        1..N -- an ``applied`` file that is half in each numbering, which is
        exactly what the all-or-none rule exists to prevent."""
        lines = _chain(" ", _SEQ_A, 1, 1) + _chain("A", _SEQ_B, 1, 100)
        design = "\n".join(lines) + "\n"
        reference = {"": _ref_chain(_SEQ_A, 500), "A": _ref_chain(_SEQ_B, 700)}
        out, rep = rp.restore_design_numbering(design, ["", "A"], reference)
        assert rep["applied"] is True, rep["reason"]
        got = rp.pdb_ca_sequence(out)
        assert [r for r, _i, _n in got[""]] == list(range(500, 520))
        assert [r for r, _i, _n in got["A"]] == list(range(700, 720))


class TestRestoreDesignNumberingCarriesInsertionCodes:
    """THE BLOCKER, end to end.

    An input chain with insertion codes -- which is every Kabat/Chothia-numbered
    antibody, i.e. the target market -- used to come back with several design
    residues stamped with the SAME number and a blank icode. It cleared the 0.9
    identity floor (0.985 on a 200-residue chain with three of them), reported
    ``applied``, and the payload still said ``target_numbering: "input"``. A
    delivered file with duplicate residue ids is strictly worse than the 1..N it
    replaced, because 1..N at least keys uniquely.
    """

    # Input chain A: 100, 100A, 100B, then 101..117. 20 residues, 20 distinct
    # names, three of them sharing residue number 100.
    _ICODES = ["", "A", "B"] + [""] * 17
    _NUMBERS = [100, 100, 100] + list(range(101, 118))

    def _reference(self):
        ref = _reference()
        ref["A"] = [(n, i, nm) for n, i, nm
                    in zip(self._NUMBERS, self._ICODES, _SEQ_A)]
        return ref

    def _restored(self):
        return rp.restore_design_numbering(_design(), ["A", "B"], self._reference())

    def test_it_applies(self):
        _out, rep = self._restored()
        assert rep["applied"] is True, rep["reason"]

    def test_no_two_residues_end_up_with_the_same_id(self):
        out, _rep = self._restored()
        ids = [(l[21:22], l[22:26], l[26:27]) for l in out.split("\n")
               if l[:6] == "ATOM  " and l[12:16].strip() == "CA"]
        assert len(ids) == len(set(ids)), "the delivered file has duplicate residues"

    def test_the_insertion_codes_reach_the_delivered_file(self):
        out, _rep = self._restored()
        assert {"A100", "A100A", "A100B"} <= _keys(out)

    def test_the_icode_column_is_actually_written(self):
        """Not merely "the parser agrees with itself": column 27 of the three
        residues numbered 100 must literally read ' ', 'A', 'B'."""
        out, _rep = self._restored()
        at_100 = [l[26:27] for l in out.split("\n")
                  if l[:6] == "ATOM  " and l[21:22] == "A" and l[22:26] == " 100"]
        assert at_100 == [" ", "A", "B"]

    def test_the_positional_correspondence_is_not_scrambled(self):
        """The sort tie-break, seen from the delivered file. Every design
        residue must keep its own NAME while taking the input's id, so residue
        i of the output names the same amino acid as residue i of the input."""
        out, _rep = self._restored()
        got = rp.pdb_ca_sequence(out)["A"]
        assert got == self._reference()["A"]

    def test_line_widths_survive_an_icode_being_written(self):
        design = _design()
        out, _rep = self._restored()
        assert ([len(l) for l in out.split("\n")]
                == [len(l) for l in design.split("\n")])


class TestRestoreDesignNumberingRefuses:
    def _unchanged(self, design, chains, reference):
        out, rep = rp.restore_design_numbering(design, chains, reference)
        assert rep["applied"] is False, rep["reason"]
        assert out == design
        return rep

    def test_a_length_mismatch_refuses(self):
        # A binder relabelled onto the target's chain id is caught here first.
        rep = self._unchanged(_design(seq_a=_SEQ_A[:15]), ["A", "B"], _reference())
        assert "length differs" in rep["reason"]

    def test_a_sequence_mismatch_refuses(self):
        # Same length, different protein: the positional map must not certify.
        rep = self._unchanged(
            _design(seq_a=list(reversed(_SEQ_A))), ["A", "B"], _reference())
        assert "sequence identity" in rep["reason"]

    def test_an_all_unknown_chain_refuses(self):
        # UNK compares equal to anything, so without the informative floor this
        # chain scores identity 1.0 against any reference of the same length.
        rep = self._unchanged(_design(seq_a=["UNK"] * 20), ["A", "B"], _reference())
        assert "informative" in rep["reason"]

    def test_all_target_chains_map_or_none_is_applied(self):
        # A alone would map cleanly; B is broken, so A must NOT be rewritten.
        rep = self._unchanged(_design(seq_b=_SEQ_B[:9]), ["A", "B"], _reference())
        assert "chain B" in rep["reason"]

    def test_a_chain_missing_from_the_design_refuses(self):
        rep = self._unchanged(_design(), ["A", "B", "D"], _reference())
        assert "absent from the design output" in rep["reason"]

    def test_a_design_missing_every_target_chain_is_not_called_already_correct(self):
        # [] == [] would vote "already in the input numbering" and hand back the
        # reassuring answer for a design that is in fact unusable.
        binder_only = "\n".join(_chain("C", _SEQ_C, 1)) + "\n"
        rep = self._unchanged(binder_only, ["A", "B"], _reference())
        assert rep["already_input_numbering"] is False
        assert "absent from the design output" in rep["reason"]

    def test_a_chain_missing_from_the_input_refuses(self):
        ref = _reference()
        ref.pop("B")
        rep = self._unchanged(_design(), ["A", "B"], ref)
        assert "absent from the input target" in rep["reason"]

    def test_a_residue_number_too_wide_for_four_columns_refuses(self):
        # 99990+ would overflow cols 23-26 and silently shift the iCode column.
        rep = self._unchanged(_design(), ["A", "B"], _reference(a_first=99990))
        assert "four columns" in rep["reason"]

    def test_an_insertion_code_too_wide_for_one_column_refuses(self):
        """The other half of the same guard. resSeq gets four columns and iCode
        gets exactly one; a two-character code has nowhere to go, and writing it
        anyway would push every field to its right along by one."""
        ref = _reference()
        ref["A"] = [(234 + i, "AB" if i == 3 else "", nm)
                    for i, nm in enumerate(_SEQ_A)]
        rep = self._unchanged(_design(), ["A", "B"], ref)
        assert "single column" in rep["reason"]

    def test_a_map_that_is_not_one_to_one_refuses(self):
        """THE BACKSTOP. Two design residues mapping onto one input residue id
        emits a file with duplicate residues in it. ``pdb_ca_sequence`` cannot
        produce such a reference any more -- it de-dupes on the full
        ``(chain, resseq, icode)`` -- so this is reachable only by handing the
        function a reference built some other way, which is precisely the
        situation a backstop is for: the shipped version of this code got here
        by dropping one field from that tuple."""
        ref = _reference()
        # Two entries with the SAME (resseq, icode): distinct residues in the
        # list, one destination id.
        ref["A"] = [(234, "", nm) if i < 2 else (234 + i, "", nm)
                    for i, nm in enumerate(_SEQ_A)]
        rep = self._unchanged(_design(), ["A", "B"], ref)
        assert "one-to-one" in rep["reason"]

    def test_a_residue_with_no_ca_is_not_left_behind_on_upstreams_number(self):
        """F2. The injectivity refusal guards the MAP; nothing guarded the
        OUTPUT. ``pdb_ca_sequence`` only sees residues that have a ``CA``, so a
        residue modelled without one is not a key in the map — and the rewrite
        loop used to hand any such coordinate record straight through, keeping
        upstream's number while every neighbour moved, with ``applied`` still
        True and the payload still claiming ``target_numbering: "input"``.

        Here the CA-less residue is numbered 234, which is what design residue
        1 becomes. The delivered file then carries TWO different residues at
        ``A234`` — a duplicate residue id, which is precisely the outcome the
        one-to-one refusal exists to prevent, reached around the side.
        """
        stray = _atom(998, "N", "TRP", "A", 234)
        design = _design() + stray + "\n"
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "not in the map" in rep["reason"], rep["reason"]
        assert "chain A" in rep["reason"]

    def test_a_hetatm_on_a_target_chain_is_not_left_behind(self):
        """F2, the second way in. A structural zinc, a ligand or an ion sits on
        a target chain as a ``HETATM`` whose residue name is not a modified
        amino acid, so ``pdb_ca_sequence`` skips it and it is not a map key.
        Numbered 240 it survives the rewrite unchanged and collides with what
        design residue 7 becomes.

        Production stages a cropped target that has no ligands in it, so this
        is a correctness hole rather than a live incident — but a rewrite that
        can emit duplicate residue ids must not report that it restored the
        operator's numbering.
        """
        design = _design() + _atom(997, "ZN", "ZN", "A", 240, record="HETATM") + "\n"
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "not in the map" in rep["reason"], rep["reason"]
        assert "240" in rep["reason"], rep["reason"]

    def test_the_refusal_counts_every_residue_it_could_not_place(self):
        """One reason line for a whole file, with the count in it — a per-line
        refusal would say "a coordinate record" and leave the operator to
        discover the other forty by hand. Every id here is one the rewrite is
        moving another residue ONTO, which is what makes leaving them in place
        a collision rather than a coexistence."""
        design = (_design()
                  + _atom(996, "N", "TRP", "A", 240) + "\n"
                  + _atom(995, "C", "TRP", "A", 241) + "\n"
                  + _atom(994, "O", "TRP", "B", 305) + "\n")
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "3 residue" in rep["reason"], rep["reason"]

    def test_the_refusal_counts_residues_rather_than_coordinate_records(self):
        """G4. The count and the sample are the only parts of this an operator
        can act on, and they described RECORDS. ONE tryptophan modelled without
        a CA, with 8 atoms and their 8 ``ANISOU`` lines, reported ``16
        coordinate record(s)`` with the sample ``(chain A residue 240, chain A
        residue 240, chain A residue 240, ...)`` — the same residue three times,
        and an ellipsis promising thirteen more when there is one.
        """
        atoms = []
        for i, name in enumerate(("N", "C", "O", "CB", "CG", "CD1", "CD2", "CE2")):
            atoms.append(_atom(900 + i, name, "TRP", "A", 240))
            atoms.append(_atom(900 + i, name, "TRP", "A", 240, record="ANISOU"))
        design = _design() + "\n".join(atoms) + "\n"
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "1 residue" in rep["reason"], rep["reason"]
        assert rep["reason"].count("chain A residue 240") == 1, rep["reason"]
        assert "..." not in rep["reason"], rep["reason"]

    def test_a_ter_too_short_to_carry_the_residue_id_refuses(self):
        """F5, from the caller. ``_splice_resid`` promises to be
        length-preserving or to return None, and the caller refuses the whole
        file on None. A 22-character chain-bearing TER is the discriminator:
        correct code refuses it, while a bounds check one column out returns a
        26-character line and the caller accepts a file that grew.
        """
        lines = _design(ter=False).rstrip("\n").split("\n")
        design = "\n".join(lines + ["TER      61      VAL A"]) + "\n"
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "too short at 22 characters" in rep["reason"], rep["reason"]

    def test_annotation_records_carrying_residue_numbers_make_it_decline(self):
        design = _design() + "HELIX    1   1 ALA A    1  GLY A   10  1\n"
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "HELIX" in rep["reason"]

    def test_a_het_record_makes_it_decline(self):
        """``HET`` names a residue by number in columns 14-17 the same way
        ``HETATM`` does, and it was missing from the list."""
        design = _design() + "HET     NAG  A 401      14\n"
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "HET" in rep["reason"]

    def test_remark_465_makes_it_decline(self):
        """REMARK 465 tabulates the residues that are MISSING from the
        coordinates, by number. Renumbering the coordinates and leaving that
        table alone produces a file that contradicts itself."""
        design = _design() + "REMARK 465   M RES C SSSEQI\n"
        rep = self._unchanged(design, ["A", "B"], _reference())
        assert "REMARK 465" in rep["reason"]

    def test_an_ordinary_remark_does_not_make_it_decline(self):
        """The companion, and the reason REMARK is not simply in the list:
        every real PDB carries REMARKs, so refusing on the record name alone
        would disable the restore on any file that has one."""
        design = _design() + "REMARK   2 RESOLUTION.    1.90 ANGSTROMS.\n"
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is True, rep["reason"]
        assert out != design

    def test_no_target_chains_named_is_a_no_op(self):
        rep = self._unchanged(_design(), [], _reference())
        assert "no target chains" in rep["reason"]

    def test_it_never_raises_and_never_loses_the_design(self):
        # A design that reaches this function has already been paid for; losing
        # it over a numbering nicety would be far worse than shipping 1..N.
        for bad in (None, 12, object()):
            out, rep = rp.restore_design_numbering(bad, ["A"], _reference())
            assert out is bad
            assert rep["applied"] is False
            assert rep["reason"]


class TestRestoreDesignNumberingUnmappedRecords:
    """G1. WHICH unmapped records cost the shard its numbering, and which do not.

    The refusal above is right about the case it was built from and wrong about
    the general one. It fired on EVERY coordinate record the map has no key
    for, and gave as its reason that leaving such a record where it is "would
    emit a file with two different residues sharing one residue id" — which is
    true only when some OTHER residue is being renumbered ONTO the id that
    record already occupies.

    A ``HETATM ZN`` at ``A9000``, against a reference of 234-253, collides with
    nothing. Refusing it ships the whole shard in upstream's 1..N: chain A
    delivered as 1..20 instead of 234..253, the operator's own hotspot labels
    stop resolving, and the results page raises a warning banner. One benign
    heteroatom for the entire feature, on a stated reason that is false about
    that input.

    So the test of the guarantee has to be the guarantee itself — no two
    different residues on one residue id in the DELIVERED bytes — rather than
    the count of records the map happened not to know about.
    """

    def _applied(self, design, chains=("A", "B"), reference=None):
        out, rep = rp.restore_design_numbering(
            design, list(chains), _reference() if reference is None else reference)
        assert rep["applied"] is True, rep["reason"]
        assert not _duplicate_residue_ids(out), _duplicate_residue_ids(out)
        return out, rep

    def _refused(self, design, chains=("A", "B"), reference=None):
        out, rep = rp.restore_design_numbering(
            design, list(chains), _reference() if reference is None else reference)
        assert rep["applied"] is False, "expected a refusal"
        assert out == design
        return rep

    # -- the benign record --------------------------------------------------

    def test_a_heteroatom_that_collides_with_nothing_still_applies(self):
        """``A9000`` is outside 234-253, so no residue is being renumbered onto
        it and it can simply stay where it is while its neighbours move."""
        zn = _atom(997, "ZN", "ZN", "A", 9000, record="HETATM")
        design = _design() + zn + "\n"
        out, _rep = self._applied(design)
        got = rp.pdb_ca_sequence(out)
        assert [r for r, _i, _n in got["A"]] == list(range(234, 254))
        assert [r for r, _i, _n in got["B"]] == list(range(300, 320))

    def test_the_benign_heteroatom_keeps_its_own_number(self):
        """It is not a key in the map, so there is nothing to move it to — and
        inventing one would be the corruption the refusal was guarding
        against."""
        zn = _atom(997, "ZN", "ZN", "A", 9000, record="HETATM")
        out, _rep = self._applied(_design() + zn + "\n")
        kept = [l for l in out.split("\n") if l[:6] == "HETATM"]
        assert kept == [zn], kept

    def test_the_operators_hotspots_still_resolve_beside_a_benign_heteroatom(self):
        """The cost of over-refusing, stated the way the operator meets it."""
        zn = _atom(997, "ZN", "ZN", "A", 9000, record="HETATM")
        out, _rep = self._applied(_design() + zn + "\n")
        assert "A241" in _keys(out)
        assert "B305" in _keys(out)

    # -- the record that really does collide --------------------------------

    def test_a_heteroatom_sitting_on_a_destination_id_still_refuses(self):
        """``A240`` IS in 234-253, so design residue 7 is being renumbered onto
        the id the zinc already holds. Leaving it there is the duplicate."""
        design = _design() + _atom(997, "ZN", "ZN", "A", 240, record="HETATM") + "\n"
        rep = self._refused(design)
        assert "240" in rep["reason"], rep["reason"]
        assert "not in the map" in rep["reason"], rep["reason"]

    def test_a_destination_id_on_another_chain_is_not_a_collision(self):
        """THE PER-CHAIN SCOPING OF THAT TEST, PINNED — and it was pinned by
        nothing at all.

        The two reference chains here occupy DISJOINT ranges, 234-253 and
        300-319. ``A305`` is therefore not a destination on chain A: it is one
        on chain B, and chain B is a different chain, so no residue is being
        renumbered onto the zinc and it can stay where it is exactly as
        ``A9000`` does.

        Pooling both chains' destinations into one set is FAIL-CLOSED, so every
        refusal test in this class still passes under it and the full 870-test
        proteina suite stayed green when it was tried. What it actually does is
        bring back the over-refusal this class exists to remove. On the real Fc
        target the two chains overlap (234-444 and 237-444) so the pooled set
        is almost the same set and the bug barely shows; on a target whose
        chains sit in disjoint ranges it silently costs the whole shard its
        numbering again.
        """
        zn = _atom(997, "ZN", "ZN", "A", 305, record="HETATM")
        out, _rep = self._applied(_design() + zn + "\n")
        assert [r for r, _i, _n in rp.pdb_ca_sequence(out)["A"]] == list(
            range(234, 254))
        assert [r for r, _i, _n in rp.pdb_ca_sequence(out)["B"]] == list(
            range(300, 320))
        assert [l for l in out.split("\n") if l[:6] == "HETATM"] == [zn]
        assert "A241" in _keys(out)

    def test_a_resseq_this_rewrite_cannot_read_does_not_cost_the_numbering(self):
        """A residue number that is not a number cannot be a destination
        either: every id this rewrite writes comes out of ``f"{n:4d}"``, and
        ``int`` reads every one of those back. So no residue can be renumbered
        onto such a record, it collides with nothing, and it keeps its own
        field while the rest of the chain moves."""
        broken = _atom(993, "N", "TRP", "A", 1)
        broken = broken[:22] + "**** " + broken[27:]
        out, _rep = self._applied(_design() + broken + "\n")
        assert broken in out.split("\n"), "the unreadable field was rewritten"
        assert [r for r, _i, _n in rp.pdb_ca_sequence(out)["A"]] == list(
            range(234, 254))

    # -- the guarantee itself, over the delivered bytes ---------------------

    def test_the_rewrite_never_creates_a_shared_residue_id(self):
        """THE PROPERTY, ASSERTED DIRECTLY OVER THE DELIVERED BYTES. Every
        refusal in this file is a means to this end, and a means can be wrong
        about its end — this one was wrong in both directions at once, refusing
        inputs that were fine on a stated reason that was false about them.

        Each design here is free of duplicate ids BEFORE the rewrite and each
        one must be genuinely rewritten, so a duplicate afterwards is one the
        rewrite created. ``applied`` is asserted for exactly that reason: a
        version that refuses everything satisfies "no duplicates" trivially,
        which is how the over-refusal this test was written for would have
        passed it.
        """
        icode_ref = _reference()
        icode_ref["A"] = _ref_chain(_SEQ_A[:3], 100, icodes=["", "A", "B"]) + \
            _ref_chain(_SEQ_A[3:], 101)
        cases = {
            "plain": (_design(), _reference()),
            "benign heteroatom": (
                _design() + _atom(997, "ZN", "ZN", "A", 9000, record="HETATM")
                + "\n", _reference()),
            "ordinary REMARK": (
                _design() + "REMARK   2 RESOLUTION.    1.90 ANGSTROMS.\n",
                _reference()),
            "insertion codes": (_design(), icode_ref),
            # Destinations that OVERLAP the design's own numbers: 1..20 -> 15..34
            # shares eleven ids with itself, so a rewrite that walked the file
            # twice, or spliced in place, would land residues on each other.
            "destinations overlapping the source": (_design(),
                                                    _reference(a_first=15)),
        }
        for label, (design, reference) in cases.items():
            assert not _duplicate_residue_ids(design), label
            out, rep = rp.restore_design_numbering(design, ["A", "B"], reference)
            assert rep["applied"] is True, f"{label}: {rep['reason']}"
            assert not _duplicate_residue_ids(out), (
                f"{label}: {_duplicate_residue_ids(out)}")

    def test_a_duplicate_upstream_already_emitted_is_carried_not_created(self):
        """THE LIMIT OF THAT PROPERTY, STATED RATHER THAN IMPLIED, because it
        looks at first like a hole in it.

        A zinc numbered ``A5`` IS a key in the map, so it is renumbered to 238
        along with design residue 5 and the delivered file carries ``ATOM ...
        VAL A 238`` beside ``HETATM ... ZN A 238``. That is a shared residue id
        in an ``applied`` file — but it is one the design ARRIVED with, at
        ``A5``, and refusing would hand the operator the same two residues on
        the same id with their hotspots no longer resolving. Strictly worse.

        The mapped path cannot create a duplicate: the map is injective, so two
        records reach one destination only if they already shared a source id.
        Only the UNMAPPED path can, and that is what the refusal above is for.
        """
        design = _design() + _atom(997, "ZN", "ZN", "A", 5, record="HETATM") + "\n"
        assert _duplicate_residue_ids(design) == [("A", "5", "")]
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is True, rep["reason"]
        assert _duplicate_residue_ids(out) == [("A", "238", "")]
        assert "A241" in _keys(out)

    def test_the_property_check_can_actually_see_a_duplicate(self):
        """The helper above proves nothing unless it FAILS on a file that has
        the defect. This is one: two different residues, both ``A238``."""
        bad = (_design()
               + _atom(997, "ZN", "ZN", "A", 238, record="HETATM") + "\n"
               + _atom(998, "CA", "TRP", "A", 238) + "\n")
        assert _duplicate_residue_ids(bad) == [("A", "238", "")]

    def test_the_property_check_does_not_call_one_residue_two(self):
        """...and it must not fire on an ordinary residue whose atoms and
        ``ANISOU`` lines share an id, or it would refuse every real file."""
        lines = []
        for i, name in enumerate(("N", "CA", "C", "O")):
            lines.append(_atom(900 + i, name, "TRP", "A", 240))
            lines.append(_atom(900 + i, name, "TRP", "A", 240, record="ANISOU"))
        assert _duplicate_residue_ids("\n".join(lines) + "\n") == []

    @pytest.mark.parametrize(
        "record", ["ATOM  ", "HETATM", "ANISOU", "SIGATM", "SIGUIJ"])
    def test_the_property_check_looks_at_every_coordinate_record(self, record):
        """...and it has to see the duplicate in ALL FIVE of them, or the
        property it checks is narrower than the rewrite it is checking.

        ``_COORD_RECORDS`` is written out rather than read off
        ``rp._RESSEQ_COORD_RECORDS`` so it cannot shrink WITH the code. Nothing
        caught it shrinking ON ITS OWN: cutting it to ``("ATOM  ", "HETATM")``
        survived the entire proteina suite. The test above looks like it would
        catch that and does not — drop ``ANISOU`` and the remaining ``ATOM``
        lines simply become contiguous, so the helper still returns ``[]`` and
        the assertion still holds.

        The record types are SPELLED OUT here too, for the reason they are
        spelled out there: parametrising on the tuple under test would delete a
        case along with its entry and leave the file green.

        This is about the DETECTOR's breadth only. Whether production renumbers
        each of these record types is pinned separately, by
        ``TestRestoreDesignNumberingRecordCoverage``.
        """
        bad = (_atom(997, "ZN", "ZN", "A", 238, record=record) + "\n"
               + _atom(998, "CA", "TRP", "A", 238, record=record) + "\n")
        assert _duplicate_residue_ids(bad) == [("A", "238", "")]


class TestRestoreDesignNumberingRecordCoverage:
    """Which records the rewrite touches, and which it must not.

    Every one of these was droppable from ``_RESSEQ_COORD_RECORDS`` without a
    single test noticing, and each drop leaves a file whose coordinate section
    and its own annotations disagree about which residue is which.
    """

    def _renumbered_line(self, record):
        """The rewritten line for a single extra record on chain A residue 1."""
        extra = _atom(999, "CA", "ALA", "A", 1, record=record)
        design = _design() + extra + "\n"
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is True, rep["reason"]
        return [l for l in out.split("\n") if l[:6] == record][-1]

    @pytest.mark.parametrize("record", ["ANISOU", "SIGATM", "SIGUIJ", "HETATM"])
    def test_every_coordinate_record_type_is_renumbered(self, record):
        assert int(self._renumbered_line(record)[22:26]) == 234

    def test_a_record_this_rewrite_does_not_own_is_left_alone(self):
        """CONECT carries ATOM SERIAL numbers, not residue numbers. Renumbering
        it would corrupt a file that was correct."""
        design = _design() + "CONECT    1    2\n"
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is True, rep["reason"]
        assert "CONECT    1    2" in out

    # WRITTEN OUT, not read off ``_RESSEQ_ANNOTATION_RECORDS``. Parametrising on
    # the tuple under test would mean deleting an entry deletes its own test
    # case and the file stays green — which is exactly the state this replaces:
    # 8 of these 10 were droppable without a single failure.
    _ANNOTATION_RECORDS = [
        ("HELIX ", "HELIX    1   1 ALA A    1  GLY A   10  1"),
        ("SHEET ", "SHEET    1   A 2 ALA A   1  GLY A  10  0"),
        ("SSBOND", "SSBOND   1 CYS A    6    CYS A  127"),
        ("LINK  ", "LINK         O   GLY A   1                 NA    NA A 100"),
        ("CISPEP", "CISPEP   1 SER A    1    PRO A    2          0        0.00"),
        ("SITE  ", "SITE     1 AC1  3 HIS A   5  ASP A   7  SER A   9"),
        ("MODRES", "MODRES 1ABC MSE A    1  MET  SELENOMETHIONINE"),
        ("SEQADV", "SEQADV 1ABC GLY A    1  UNP  P00001    ALA     1 CONFLICT"),
        ("DBREF ", "DBREF  1ABC A    1    20  UNP    P00001   TEST     1    20"),
        ("HET   ", "HET     NAG  A 401      14"),
    ]

    @pytest.mark.parametrize("record,line", _ANNOTATION_RECORDS,
                             ids=[r.strip() for r, _ in _ANNOTATION_RECORDS])
    def test_every_annotation_record_type_makes_it_decline(self, record, line):
        """F7. Each of these tabulates residue numbers somewhere other than
        columns 23-26, so renumbering only the coordinate section leaves the
        file disagreeing with itself about which residue is which. The restore
        declines instead — the direction that ships upstream's file untouched.
        """
        design = _design() + line + "\n"
        out, rep = rp.restore_design_numbering(design, ["A", "B"], _reference())
        assert rep["applied"] is False, rep["reason"]
        assert out == design
        assert record.strip() in rep["reason"], rep["reason"]


class TestChainRenumberMap:
    def test_it_maps_position_for_position(self):
        obs = _ref_chain(_SEQ_A, 1)
        ref = _ref_chain(_SEQ_A, 234)
        got = rp.chain_renumber_map(obs, ref)
        assert got["ok"] is True
        assert got["map"][(1, "")] == (234, "")
        assert got["map"][(20, "")] == (253, "")
        assert got["identity"] == 1.0

    def test_the_map_is_keyed_and_valued_on_the_whole_residue_id(self):
        """``{resseq: resseq}`` is not a map between PDB residues -- it drops
        the insertion code from both ends, which is how three input residues
        collapsed onto one output number."""
        obs = _ref_chain(_SEQ_A, 1)
        ref = _ref_chain(_SEQ_A[:3], 100, icodes=["", "A", "B"]) + \
            _ref_chain(_SEQ_A[3:], 101)
        got = rp.chain_renumber_map(obs, ref)
        assert got["ok"] is True
        assert got["map"][(2, "")] == (100, "A")
        assert got["map"][(3, "")] == (100, "B")

    def test_an_empty_chain_refuses(self):
        assert rp.chain_renumber_map([], [])["ok"] is False

    def test_a_stray_unknown_does_not_drag_a_real_chain_below_the_floor(self):
        obs = _ref_chain(["UNK"] + _SEQ_A[1:], 1)
        ref = _ref_chain(_SEQ_A, 234)
        got = rp.chain_renumber_map(obs, ref)
        assert got["ok"] is True
        assert got["n_informative"] == 19

    def test_a_few_lucky_matches_among_unknowns_still_refuses(self):
        # identity 1.0 over 3 informative pairs is not evidence of a chain.
        obs = _ref_chain(["UNK"] * 17 + _SEQ_A[17:], 1)
        ref = _ref_chain(_SEQ_A, 234)
        got = rp.chain_renumber_map(obs, ref)
        assert got["n_informative"] == 3
        assert got["identity"] == 1.0
        assert got["ok"] is False
        assert "informative" in got["reason"]

    def test_a_mostly_unknown_reference_no_longer_certifies_a_whole_chain(self):
        """CHANGED DELIBERATELY, and this comment is the argument for it.

        This test used to be ``test_an_all_unknown_reference_may_still_certify_
        a_tiny_target`` and asserted ``ok is True``: a reference offering only 3
        informative residues lowered the absolute floor to 3, so 3 coincidental
        matches certified a map over the whole chain. The floor is
        ``max(1, min(10, ref_informative))``, which on a 200-residue reference
        with 198 UNK collapses to 2 -- two matches then key 200 residues onto
        the operator's numbering.

        The cap on the absolute floor is right and stays: a genuinely tiny
        target must still work, and it does (see the test below). What was
        missing is the canary's second half, ``TARGET_MIN_INFORMATIVE_FRACTION
        = 0.5``, which production never ported. A chain that is 85% unknown is
        mostly wildcard matches rather than evidence, whatever the absolute
        count says.
        """
        seq = ["UNK"] * 17 + _SEQ_A[17:]
        obs = _ref_chain(seq, 1)
        ref = _ref_chain(seq, 234)
        got = rp.chain_renumber_map(obs, ref)
        assert got["ok"] is False
        assert "3 of the 20" in got["reason"]

    def test_a_genuinely_tiny_target_still_certifies(self):
        """The case the absolute floor's cap exists for, and the boundary the
        fraction floor must not swallow: a 6-residue target, every name known.
        Below the absolute minimum of 10 and comfortably above 50% evidence."""
        obs = _ref_chain(_SEQ_A[:6], 1)
        ref = _ref_chain(_SEQ_A[:6], 234)
        assert rp.chain_renumber_map(obs, ref)["ok"] is True

    def test_the_informative_fraction_floor_is_exactly_one_half(self):
        """Pins the VALUE, not just the presence. 10 of 20 informative is
        exactly 0.5 and passes; 9 of 20 is below and refuses."""
        ref = _ref_chain(_SEQ_A, 234)
        at_floor = _ref_chain(["UNK"] * 10 + _SEQ_A[10:], 1)
        below = _ref_chain(["UNK"] * 11 + _SEQ_A[11:], 1)
        assert rp.chain_renumber_map(at_floor, ref)["ok"] is True
        assert rp.chain_renumber_map(below, ref)["ok"] is False

    def test_a_modified_residue_compares_equal_to_its_parent(self):
        """B5. An upstream refold writes selenomethionine back as METHIONINE.
        Exact ``==`` scores every one of those a mismatch, so a target with
        enough of them drops below 0.9 and the operator silently receives 1..N
        -- the defect this whole function exists to fix, firing on a correct
        input. MSE and MET are the same residue."""
        seq = ["MSE" if nm == "MET" else nm for nm in _SEQ_A]
        obs = _ref_chain(seq, 1)          # design: refolded, MSE -> MET
        ref = _ref_chain(_SEQ_A, 234)     # input: the deposited MSE
        got = rp.chain_renumber_map(obs, _ref_chain(seq, 234))
        assert got["identity"] == 1.0
        folded = rp.chain_renumber_map(obs, ref)
        assert folded["ok"] is True, folded["reason"]
        assert folded["identity"] == 1.0

    # F6. THE TABLE'S CONTENT, WRITTEN OUT INDEPENDENTLY. The test this replaces
    # iterated ``rp._MODRES_PARENT`` and asserted ``_same_resname(child,
    # parent)`` for each entry — but ``_same_resname`` IS that table, so the
    # assertion was ``table[x] == table[x]``. Deleting PTR, KCX, HYP, LLP and
    # CSD survived it; so did changing CSO's parent from CYS to TRP. Its
    # docstring claimed it would catch a missing entry, which is the one thing
    # it could not do.
    #
    # Each parent below is the amino acid the modification is made FROM, which
    # is what an upstream refold writes back: MSE is methionine with selenium,
    # SEP/TPO/PTR are phosphoserine/threonine/tyrosine, KCX is carboxylated
    # lysine, HYP is hydroxyproline, LLP is the lysine-PLP Schiff base, PCA is
    # pyroglutamate (from GLU), and the CYS block is the oxidised / alkylated
    # cysteines.
    _EXPECTED_MODRES_PARENT = {
        "MSE": "MET", "CME": "CYS", "CSO": "CYS", "SEP": "SER", "TPO": "THR",
        "PTR": "TYR", "KCX": "LYS", "HYP": "PRO", "LLP": "LYS", "CSD": "CYS",
        "OCS": "CYS", "MLY": "LYS", "M3L": "LYS", "CAS": "CYS", "CSS": "CYS",
        "CSX": "CYS", "PCA": "GLU", "SAC": "SER",
    }

    def test_the_modified_residue_table_has_the_entries_it_claims_to(self):
        """Pins the CONTENT, so a deletion or a wrong parent fails here. An
        entry silently missing is not a cosmetic loss: an upstream refold that
        writes the modification back as its parent then scores every one of
        those a mismatch, and a target with enough of them drops below the 0.9
        floor and ships in 1..N with nothing said."""
        assert rp._MODRES_PARENT == self._EXPECTED_MODRES_PARENT

    def test_every_modified_residue_upstream_accepts_has_a_parent(self):
        """The two structures live in different places and neither imports the
        other. ``_MODRES_EQUIV`` decides what counts as a protein residue at
        parse time; ``_MODRES_PARENT`` decides what it compares equal to. A
        name in the first and not the second is a residue that is counted and
        then always scored a mismatch."""
        assert set(rp._MODRES_PARENT) == set(rp._MODRES_EQUIV)

    @pytest.mark.parametrize("child,parent", sorted(_EXPECTED_MODRES_PARENT.items()))
    def test_each_modified_residue_folds_to_its_parent(self, child, parent):
        """The behavioural half, driven from the written-out expectation rather
        than from the table it is testing."""
        assert rp._same_resname(child, parent), f"{child} does not fold to {parent}"
        assert rp._same_resname(parent, child)

    def test_two_different_residues_still_do_not_compare_equal(self):
        """The folding must not become a wildcard: it is what stops a false
        MATCH from rewriting the deliverable's keys on a correspondence that
        does not hold."""
        assert rp._same_resname("MSE", "CYS") is False
        assert rp._same_resname("ALA", "GLY") is False

    def test_the_identity_floor_is_exactly_zero_point_nine(self):
        """Pins the VALUE and the BOUNDARY. 0.9 -> 0.45 and ``<`` -> ``<=``
        both used to survive the suite, so neither the number nor the sense of
        the comparison was defended by anything.

        20 residues, so identity moves in steps of 0.05: 18/20 is exactly 0.9
        and must PASS (the comparison is ``identity < min_identity``), 17/20 is
        0.85 and must refuse. A floor of 0.45 would accept both.
        """
        ref = _ref_chain(_SEQ_A, 234)
        # Two mismatches: 18/20 = 0.90 exactly.
        at_floor = _ref_chain(["GLY", "ALA"] + _SEQ_A[2:], 1)
        # Three mismatches: 17/20 = 0.85.
        below = _ref_chain(["GLY", "ALA", "TRP"] + _SEQ_A[3:], 1)
        assert rp.chain_renumber_map(at_floor, ref)["identity"] == 0.9
        assert rp.chain_renumber_map(at_floor, ref)["ok"] is True
        assert rp.chain_renumber_map(below, ref)["identity"] == 0.85
        assert rp.chain_renumber_map(below, ref)["ok"] is False

    def test_an_injective_map_is_required(self):
        obs = _ref_chain(_SEQ_A, 1)
        ref = [(234, "", nm) if i < 2 else (234 + i, "", nm)
               for i, nm in enumerate(_SEQ_A)]
        got = rp.chain_renumber_map(obs, ref)
        assert got["ok"] is False
        assert got["map"] == {}
        assert "one-to-one" in got["reason"]


class TestSpliceResid:
    """F5. The two SHORT-LINE branches, which no test ever reached.

    ``_splice_resid`` promises to be length-preserving or to return ``None``,
    and the caller refuses the whole file on ``None`` rather than emit a record
    whose fields have all shifted right. Every fixture in this file is 80
    columns wide, so both short-line branches were dead in every test and two
    mutations survived the suite — ``len(line) < 26`` weakened to ``< 22``, and
    the 26-character branch made to splice unconditionally. Both make the
    function GROW a line, which in a fixed-column format is a corrupted file.

    26 and 22 characters are not invented widths: the archived input target's
    own TER records are 26 characters plus a trailing space.
    """

    # cols: TER(0-2) serial(7-10) resName(17-19) chainID(21) resSeq(22-25)
    _TER26 = "TER    1234      VAL A 211"
    # The same record with the resSeq field itself truncated away.
    _TER22 = "TER      61      VAL A"

    def test_the_fixtures_are_the_widths_this_class_is_named_for(self):
        assert len(self._TER26) == 26
        assert len(self._TER22) == 22

    def test_a_line_too_short_for_the_resseq_field_is_refused(self):
        """Nothing can be written into a field that is not there. A bounds
        check one column out would return ``line[:22] + "%4d"`` — a 26-character
        line built out of a 22-character one."""
        assert rp._splice_resid(self._TER22, (234, "")) is None
        assert rp._splice_resid(self._TER22, (234, "A")) is None

    def test_a_twenty_six_character_line_is_rewritten_without_growing(self):
        """resSeq is complete but the file ends before the iCode column.
        Writing a BLANK icode into a column that does not exist is a no-op, so
        the line is rewritten without one — and stays 26 characters."""
        got = rp._splice_resid(self._TER26, (234, ""))
        assert got is not None
        assert len(got) == 26
        assert got == "TER    1234      VAL A 234"

    def test_a_twenty_six_character_line_cannot_carry_an_insertion_code(self):
        """A REAL icode has nowhere to go here. Splicing it anyway appends a
        27th column, which is how a rewrite that "only touches columns 23-27"
        moves every field of the record it did not touch."""
        assert rp._splice_resid(self._TER26, (234, "A")) is None

    def test_a_full_width_line_keeps_both_fields_and_its_width(self):
        """The ordinary branch, stated next to the other two so the contract
        reads as one thing: 27 columns or more and both fields are written."""
        line = _atom(1, "CA", "ALA", "A", 5)
        got = rp._splice_resid(line, (234, "B"))
        assert len(got) == len(line)
        assert got[22:26] == " 234"
        assert got[26:27] == "B"
        assert got[27:] == line[27:]


class TestTheStagedReferenceEncoding:
    """G3. What a non-ASCII byte in the uploaded target actually does.

    ``stage_cropped_target`` WRITES the staged crop with ``dest.write_text``,
    i.e. the platform default encoding, and the upload loop READS it back as
    latin-1. The note that documented that asymmetry named the wrong exposure
    and drew the wrong conclusion from it, and a false comment is worse than
    no comment — this is what the replacement has to be true about.

    Behaviour is fail-closed either way, so nothing here is a code fix; these
    are the two measurements the note is now written from.
    """

    # Residue 4 is MSE, a MODIFIED RESIDUE, and it is written as a ``HETATM``
    # exactly as a real deposit writes selenomethionine. Without it this whole
    # class describes a file no real target looks like, and the record-set
    # assertion below silently became a claim about the fixture rather than
    # about the crop.
    _NAMES = ["ALA", "GLY", "SER", "MSE", "VAL", "LEU", "ILE", "PRO", "PHE",
              "TYR", "TRP", "HIS"]

    def _upload(self, mangle=()):
        """A 12-residue chain A, plus the annotation records a real deposit has.

        ``mangle`` indexes residues whose NAME field gets byte 0xE9 in its last
        column — the one place a non-ASCII byte can both survive the crop and
        land inside the fixed-width columns the restore reads.
        """
        lines = ["HEADER    TEST", "REMARK   1 AUTH   J. M\xe9LLER",
                 "SEQRES   1 A   12  ALA GLY SER"]
        for i, name in enumerate(self._NAMES):
            record = "HETATM" if name in rp._MODRES_EQUIV else "ATOM  "
            atom = _atom(i + 1, "CA", name, "A", i + 1, record=record)
            if i in mangle:
                atom = atom[:17] + name[:2] + "\xe9" + atom[20:]
            lines.append(atom)
        return "\n".join(lines) + "\nEND\n"

    def _stage(self, tmp_path, text, name):
        raw = tmp_path / f"{name}.pdb"
        raw.write_bytes(text.encode("latin-1"))
        residues, _ = rp.pdb_ca_residues(raw)
        staged = tmp_path / f"{name}_staged.pdb"
        # EXACTLY the two calls production makes, in order: the upload is read
        # with the platform default and ``errors="replace"``, and the staged
        # file is read back as latin-1.
        rp.stage_cropped_target(staged, raw.read_text(errors="replace"),
                                residues, [("A", 1, 12)])
        return rp.pdb_ca_sequence(staged.read_text(encoding="latin-1"))

    def test_the_crop_emits_no_remark_for_a_byte_to_hide_in(self, tmp_path):
        """The note named a REMARK as the exposure. ``crop_pdb_to_contig``
        emits COORDINATE lines — ``ATOM``, and ``HETATM`` for a modified
        residue in ``_MODRES_EQUIV`` — plus one ``TER`` per chain and a final
        ``END``, and no annotation record at all, so no REMARK, no HEADER and
        no SEQRES is ever in the file the restore reads.

        THE COUNTS ARE ASSERTED, NOT JUST THE RECORD SET. This test used to
        assert ``{"ATOM", "TER", "END"}`` and passed only because its fixture
        contained no modified residue — so it stood behind a production comment
        that was false about every deposit containing one. A record set alone
        goes quiet again the moment the fixture loses its ``HETATM``; the
        counts do not.
        """
        raw = tmp_path / "in.pdb"
        raw.write_bytes(self._upload().encode("latin-1"))
        residues, _ = rp.pdb_ca_residues(raw)
        staged = tmp_path / "staged.pdb"
        rp.stage_cropped_target(staged, raw.read_text(errors="replace"),
                                residues, [("A", 1, 12)])
        records = [l[:6].strip()
                   for l in staged.read_text(encoding="latin-1").split("\n") if l]
        counts = {r: records.count(r) for r in set(records)}
        assert counts == {"ATOM": 11, "HETATM": 1, "TER": 1, "END": 1}, counts

    def test_a_non_ascii_byte_in_a_kept_coordinate_line_does_move_the_reference(
            self, tmp_path):
        """...and where it CAN land, it moves exactly what the note said it
        moved "not at all". Which way depends on the platform default: under
        UTF-8 (what the container runs) the replacement character is written
        back as three bytes, so the latin-1 read finds that line two columns
        wide and the residue drops out of the reference entirely; under a
        single-byte default the widths hold and the residue NAME changes."""
        clean = self._stage(tmp_path, self._upload(), "clean")
        dirty = self._stage(tmp_path, self._upload(mangle=(1, 4)), "dirty")
        assert clean["A"] == [(i + 1, "", n) for i, n in enumerate(self._NAMES)]
        assert dirty["A"] != clean["A"], (
            "the byte reached neither the residue names nor the residue count")

    def test_and_the_restore_declines_rather_than_renumbering_against_it(
            self, tmp_path):
        """The direction that makes this a documentation defect rather than a
        live one. A design that matches the CLEAN target is refused against the
        mangled reference — on length under UTF-8, on sequence identity under a
        single-byte default. Neither renumbers, so a clean apply becomes a
        refusal and never a wrong file."""
        dirty = self._stage(tmp_path, self._upload(mangle=(1, 4)), "dirty")
        design = "\n".join(
            _atom(i + 1, "CA", n, "A", i + 1)
            for i, n in enumerate(self._NAMES)) + "\nEND\n"
        out, rep = rp.restore_design_numbering(design, ["A"], dirty)
        assert rep["applied"] is False, rep
        assert out == design
        assert ("length differs" in rep["reason"]
                or "sequence identity" in rep["reason"]), rep["reason"]


def test_the_staged_crop_encoding_note_describes_the_code_that_exists():
    """G3, as text. The note claimed the exposure was "a non-ASCII byte in a
    REMARK" — a record the crop does not emit — and concluded that it "would
    move residue NAMES not at all and identity not at all", which the class
    above measures as false for the line where such a byte can actually land.

    A comment cannot be tested for truth, only for the specific false sentences
    it was caught making. These two are the ones."""
    src = (_PROTEINA_DIR / "run_pipeline.py").read_text(encoding="utf-8")
    assert "non-ASCII byte in a REMARK" not in src, (
        "the note still names a REMARK as the exposure; crop_pdb_to_contig "
        "emits only ATOM/TER/END")
    assert "NAMES not at all and identity not at all" not in src, (
        "the note still says a non-ASCII byte moves neither, measured false "
        "by TestTheStagedReferenceEncoding")


class TestUploadLoopNumbering:
    """THE CALL SITE, exercised rather than pattern-matched.

    MERGE NOTE (parity branch). Three tests here assert the set of uploaded
    basenames as a stand-in for "both designs shipped". They were written
    against 0-based names and now read ``design_001.pdb`` / ``design_002.pdb``,
    because the delivered rank became dense and 1-based to match the other five
    generators -- the same change that makes proteina agree with
    ``shared/exports.py``'s cross-tool invariant. The filenames were never this
    class's subject; each of those tests is really asserting ``target_numbering``,
    ``n_failures`` or ``designs_completed``, and every one of those assertions is
    untouched. Nothing here was relaxed to accommodate the merge.

    Everything above this class tests pure functions. The only thing that stood
    between them and the delivered file was an AST check that
    ``restore_design_numbering`` appears somewhere in a Call node and a
    substring check for ``"target_numbering"``. Reviewer B mutated the upload
    loop in an isolated worktree and ALL SIX of these survived the whole suite:

      * deleting the renumber block outright
      * discarding ``restored`` and never assigning ``pdb_bytes``
      * setting ``numbering = "input"`` unconditionally
      * passing the BINDER chain into ``renumber_chains``
      * reading the reference from the RAW UPLOAD instead of the staged crop
      * running the restore on CURATED runs too

    These drive ``main()`` for real with the network and ``complexa`` stubbed,
    and capture the bytes actually handed to ``upload_pdb`` -- which is the only
    artifact the operator ever sees.
    """

    # The upload holds 40 residues per target chain; the contig selects 20 of
    # them. That gap is what tells "read the staged crop" apart from "read the
    # raw upload": against the crop the design's 20-residue chains map, against
    # the upload they fail on length.
    _CONTIG = "A11-30,B11-30"
    _UPLOAD_SPAN = (1, 40)
    _CROP = (11, 30)

    def _upload_text(self):
        lines = []
        serial = 1
        for chain, seq in (("A", _SEQ_A), ("B", _SEQ_B)):
            lo, hi = self._UPLOAD_SPAN
            for resseq in range(lo, hi + 1):
                lines.append(_atom(serial, "CA", seq[(resseq - 1) % len(seq)],
                                   chain, resseq))
                serial += 1
        return "\n".join(lines) + "\nEND\n"

    def _cropped_names(self, seq):
        lo, hi = self._CROP
        return [seq[(r - 1) % len(seq)] for r in range(lo, hi + 1)]

    def _design_text(self, *, seq_a=None, seq_b=None):
        """A design as upstream writes one: target chains renumbered to 1..N."""
        a = self._cropped_names(_SEQ_A) if seq_a is None else seq_a
        b = self._cropped_names(_SEQ_B) if seq_b is None else seq_b
        lines = []
        lines += _chain("A", a, 1, 1)
        lines.append(_ter(500, a[-1], "A", len(a)))
        lines += _chain("B", b, 1, 100)
        lines.append(_ter(501, b[-1], "B", 999))
        lines += _chain("C", _SEQ_C, 1, 200)
        lines.append(_ter(502, _SEQ_C[-1], "C", 888))
        return "\n".join(lines) + "\nEND\n"

    def _drive(self, tmp_path, monkeypatch, *, job_spec, n_designs=2,
               design_text=None, input_text=None, plant_staged=None,
               endpoint="https://example/upload"):
        """Run main() to completion, returning (result, uploaded_bytes_by_name).

        ``plant_staged`` writes a file into the hub target dir under the run's
        task_name BEFORE main() runs -- the only way to give a CURATED run a
        staged reference to read, which is what makes the "curated runs too"
        mutation observable rather than inert.

        ``endpoint=""`` drives the INLINE path instead, where nothing is
        uploaded and the coordinates come back in the result. ``uploaded`` is
        then empty by construction and the delivered bytes must be read off
        ``candidates[*]["pdb_content_b64"]``.
        """
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setattr(rp, "RAW_ARCHIVE_PATH", str(tmp_path / "raw.tgz"))
        home = tmp_path / "proteina"
        (home / "configs" / "targets").mkdir(parents=True)
        registry = home / "configs" / "targets" / "targets_dict.yaml"
        registry.write_text(
            "target_dict_cfg:\n"
            "  02_PDL1:\n"
            "    source: bindcraft_targets\n"
            "    target_path: ./assets/target_data/bindcraft_targets/PD-L1.pdb\n"
        )
        monkeypatch.setattr(rp, "PROTEINA_HOME", str(home))
        monkeypatch.setattr(rp, "_TARGETS_DICT", str(registry))
        hub_targets = home / "hub_targets"
        monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(hub_targets))
        run_dir = home / "inference"

        # write_BYTES throughout, never write_text: on Windows the text writer
        # translates "\n" to "\r\n", and the pipeline reads these files as bytes.
        # A fixture written in text mode would be comparing the delivered file
        # against a differently-terminated copy of itself.
        if plant_staged:
            hub_targets.mkdir(parents=True, exist_ok=True)
            (hub_targets / f"{plant_staged}.pdb").write_bytes(
                self._staged_equivalent().encode("latin-1"))

        text = self._upload_text() if input_text is None else input_text
        monkeypatch.setattr(
            rp, "download_target",
            lambda url, dest: (dest.parent.mkdir(parents=True, exist_ok=True),
                               dest.write_bytes(text.encode("latin-1")), dest)[-1])

        design = self._design_text() if design_text is None else design_text

        def fake_run(cmd, cwd):
            if cmd[:3] == [rp.COMPLEXA_BIN, "target", "add"]:
                import yaml
                data = yaml.safe_load(registry.read_text()) or {}
                data.setdefault("target_dict_cfg", {})
                data["target_dict_cfg"][cmd[3]] = {
                    "source": rp._HUB_SOURCE,
                    "target_path": cmd[cmd.index("--target-path") + 1],
                    "target_input": cmd[cmd.index("--target-input") + 1],
                    "hotspot_residues": [],
                    "binder_length": [
                        int(cmd[cmd.index("--binder-length") + 1]),
                        int(cmd[cmd.index("--binder-length") + 2]),
                    ],
                }
                registry.write_text(yaml.safe_dump(data, sort_keys=False))
                return 0
            # `complexa design`: emit the reward CSV and the design PDBs the
            # upload loop will read.
            out = run_dir / "samples"
            out.mkdir(parents=True, exist_ok=True)
            rows = ["sample,pdb_path,total_reward"]
            for i in range(n_designs):
                pdb = out / f"sample_{i}.pdb"
                pdb.write_bytes(design.encode("latin-1"))
                rows.append(f"sample_{i},{pdb},{1.0 - i * 0.1:.3f}")
            (run_dir / "rewards_shard.csv").write_bytes(
                ("\n".join(rows) + "\n").encode("latin-1"))
            return 0
        monkeypatch.setattr(rp, "run_streaming", fake_run)

        uploaded: dict[str, bytes] = {}
        monkeypatch.setattr(
            rp, "request_upload_urls",
            lambda endpoint, token, names: {n: f"https://up/{n}" for n in names})
        monkeypatch.setattr(
            rp, "upload_pdb",
            lambda url, pdb_bytes: uploaded.__setitem__(
                url.rsplit("/", 1)[-1], pdb_bytes))

        payload = {
            "job_spec": job_spec,
            "input_presigned_url": (
                "" if job_spec.get("target_source") != "custom"
                else "https://example/target.pdb"),
            "upload_urls_endpoint": endpoint,
            # No job_token on the inline path: a hub-shaped payload that lost
            # its endpoint is refused pre-GPU by design, so leaving it set here
            # would drive a refusal instead of an inline delivery.
            "job_token": "t" if endpoint else "", "tier": "protein_binder",
        }
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps(payload))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-upload")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        try:
            rp.main()
        except SystemExit:
            pass
        return json.loads(result_file.read_text()), uploaded

    def _staged_equivalent(self):
        """The staged crop's content, built the way the crop would build it."""
        lines, serial = [], 1
        lo, hi = self._CROP
        for chain, seq in (("A", _SEQ_A), ("B", _SEQ_B)):
            for resseq in range(lo, hi + 1):
                lines.append(_atom(serial, "CA", seq[(resseq - 1) % len(seq)],
                                   chain, resseq))
                serial += 1
        return "\n".join(lines) + "\nEND\n"

    def _custom(self, **kw):
        base = {
            "config_name": "search_binder_local_pipeline", "task_name": "",
            "target_source": "custom", "target_chain": "A",
            "target_input": self._CONTIG, "hotspot_spec": [],
            "binder_length": [60, 120],
            "rf3_required": False, "nsamples": 4, "replicas": 2,
        }
        base.update(kw)
        return base

    # -- the delivered bytes ------------------------------------------------

    def test_the_uploaded_bytes_are_renumbered(self, tmp_path, monkeypatch):
        """Kills "delete the renumber block" and "discard ``restored``". The
        assertion is on the BYTES HANDED TO upload_pdb, because that is the file
        the operator downloads -- a report that says "applied" over an
        unmodified upload is the failure being guarded against."""
        data, uploaded = self._drive(tmp_path, monkeypatch, job_spec=self._custom())
        assert data["status"] == "COMPLETED", data.get("error")
        assert uploaded, "no design was uploaded at all"
        for name, blob in uploaded.items():
            got = rp.pdb_ca_sequence(blob.decode("latin-1"))
            assert [r for r, _i, _n in got["A"]] == list(range(11, 31)), name
            assert [r for r, _i, _n in got["B"]] == list(range(11, 31)), name

    def test_the_operators_hotspot_token_resolves_in_the_delivered_file(
            self, tmp_path, monkeypatch):
        """The whole point, stated as the operator experiences it."""
        _data, uploaded = self._drive(tmp_path, monkeypatch,
                                      job_spec=self._custom())
        blob = next(iter(uploaded.values())).decode("latin-1")
        # A25 is inside the crop (11-30) and OUTSIDE the 1..20 upstream wrote,
        # so it discriminates. A15 would not: it exists in both.
        assert "A25" in _keys(blob)
        assert "A25" not in _keys(self._design_text())

    def test_the_target_chains_move_and_the_binder_chain_does_not(
            self, tmp_path, monkeypatch):
        """Kills "pass the binder chain into renumber_chains".

        F9: this test used to assert ONLY that chain C came back unchanged, and
        claimed in its docstring that it "states the property directly". It did
        not. Chain C is absent from the staged target, so adding it to
        ``renumber_chains`` makes every chain fail the all-or-none rule and
        NOTHING is rewritten — under which chain C is trivially unchanged and
        the old assertion passed. The kill came entirely from the neighbouring
        test noticing that chain A had not moved.

        The property is a conjunction and has to be asserted as one: the target
        chains carry the operator's numbers AND the binder still carries 1..N.
        """
        _data, uploaded = self._drive(tmp_path, monkeypatch,
                                      job_spec=self._custom())
        blob = next(iter(uploaded.values())).decode("latin-1")
        got = rp.pdb_ca_sequence(blob)
        assert [r for r, _i, _n in got["A"]] == list(range(11, 31))
        assert [r for r, _i, _n in got["B"]] == list(range(11, 31))
        assert got["C"] == rp.pdb_ca_sequence(self._design_text())["C"]

    def test_the_reference_is_the_staged_crop_not_the_raw_upload(
            self, tmp_path, monkeypatch):
        """Kills "read the reference from the raw upload". The upload holds 40
        residues per chain and the crop holds 20; the design's chains are 20
        long, so reading the upload fails on length and delivers 1..N. This
        passes only if the crop was read."""
        data, uploaded = self._drive(tmp_path, monkeypatch, job_spec=self._custom())
        blob = next(iter(uploaded.values())).decode("latin-1")
        assert [r for r, _i, _n in rp.pdb_ca_sequence(blob)["A"]] == list(range(11, 31))
        assert all(c["target_numbering"] == "input" for c in data["candidates"])

    def test_the_INLINE_path_delivers_the_operators_numbering_too(
            self, tmp_path, monkeypatch):
        """A GAP THE MERGE CREATED, closed here rather than left to rot.

        #123 was written when the upload was the only way a design left the
        container, so every test in this class reads the bytes out of
        ``uploaded``. The parity branch added INLINE delivery, and merging the
        two put the restore between the read and the upload -- which means it
        now applies to a design that is never uploaded at all. That is the
        behaviour an operator calling ``modal.Function.from_name`` directly
        actually gets, and nothing covered it: with the restore left on the
        upload's side of the branch, every assertion in this class would still
        pass while direct callers silently received 1..N.

        Asserts on the DECODED INLINE BYTES, not on ``target_numbering`` -- the
        label is what the code claims, the coordinates are what it did.
        """
        data, uploaded = self._drive(
            tmp_path, monkeypatch, job_spec=self._custom(), endpoint="")
        assert data["status"] == "COMPLETED", data.get("error")
        assert not uploaded, "the inline path must not upload anything"
        assert len(data["candidates"]) == 2
        for cand in data["candidates"]:
            blob = base64.b64decode(cand["pdb_content_b64"]).decode("latin-1")
            assert [r for r, _i, _n in rp.pdb_ca_sequence(blob)["A"]] == \
                list(range(11, 31)), "inline design shipped in upstream's 1..N"
            assert cand["target_numbering"] == "input"

    # -- what the payload claims -------------------------------------------

    def test_target_numbering_records_what_really_happened(
            self, tmp_path, monkeypatch):
        """Kills ``numbering = "input"`` unconditionally. A design whose target
        chain is a DIFFERENT protein cannot be renumbered, so the file ships in
        upstream's 1..N -- and the payload has to say so. A constant would read
        "input" here."""
        scrambled = self._design_text(
            seq_a=list(reversed(self._cropped_names(_SEQ_A))))
        data, uploaded = self._drive(tmp_path, monkeypatch,
                                     job_spec=self._custom(),
                                     design_text=scrambled)
        assert data["status"] == "COMPLETED", data.get("error")
        assert [c["target_numbering"] for c in data["candidates"]] == ["upstream"] * 2
        assert [d["target_numbering"] for d in data["designs"]] == ["upstream"] * 2
        # ...and the design still SHIPS, byte-for-byte as upstream wrote it.
        assert set(uploaded) == {"design_001.pdb", "design_002.pdb"}
        assert next(iter(uploaded.values())) == scrambled.encode("latin-1")

    def test_the_candidates_carry_the_numbering_not_only_the_designs(
            self, tmp_path, monkeypatch):
        """B6. shared/jobs.py::candidate_records prefers ``candidates`` and the
        results template reads ``candidates`` only, so a field written into
        ``designs`` alone is data nobody can ever see. It was."""
        data, _uploaded = self._drive(tmp_path, monkeypatch,
                                      job_spec=self._custom())
        assert data["candidates"], "no candidates were recorded"
        for cand in data["candidates"]:
            assert cand["target_numbering"] == "input"

    def test_a_byte_that_is_not_utf8_survives_the_round_trip(
            self, tmp_path, monkeypatch):
        """``decode("utf-8", errors="replace")`` is not a round trip. Every byte
        it cannot decode becomes U+FFFD, and re-encoding turns that into the
        three bytes EF BF BD -- so a design carrying a latin-1 author name, a
        degree sign, or any stray high byte comes back CORRUPTED and longer than
        it went in, by the very step that was supposed to touch nothing but
        columns 23-27. latin-1 maps all 256 byte values one-to-one.

        The byte here sits in a REMARK, outside the coordinate columns, so the
        restore still applies and the rest of the file is genuinely rewritten --
        this pins the encoding, not a decline.
        """
        design = self._design_text() + "REMARK   1 AUTH   J. M\xfcLLER\n"
        data, uploaded = self._drive(tmp_path, monkeypatch,
                                     job_spec=self._custom(),
                                     design_text=design)
        assert data["status"] == "COMPLETED", data.get("error")
        blob = next(iter(uploaded.values()))
        assert all(c["target_numbering"] == "input" for c in data["candidates"]), (
            "the file was not rewritten, so this proves nothing about encoding")
        assert b"\xfc" in blob
        assert b"\xef\xbf\xbd" not in blob
        assert len(blob) == len(design.encode("latin-1"))

    # -- one heteroatom, and what it costs the whole shard ------------------

    def _with_heteroatom(self, resseq):
        """The design, plus a ``HETATM ZN`` on target chain A at ``resseq``.

        The crop is 11-30, so 9000 is a number no residue is being renumbered
        onto and 25 is one that is. Both are records the map has no key for
        (its keys are the design's own 1..20), which is what makes them the two
        sides of the same question.
        """
        text = self._design_text()
        return text + _atom(997, "ZN", "ZN", "A", resseq, record="HETATM") + "\n"

    def test_a_benign_heteroatom_does_not_cost_the_shard_its_numbering(
            self, tmp_path, monkeypatch):
        """G1, through the real loop. The refusal this replaces fired on ANY
        coordinate record the map had no key for, so one structural zinc
        outside the reference range flipped the whole shard back to upstream's
        1..N — every design, both chains, on a stated reason ("two different
        residues sharing one residue id") that is false about this file.
        """
        data, uploaded = self._drive(tmp_path, monkeypatch,
                                     job_spec=self._custom(),
                                     design_text=self._with_heteroatom(9000))
        assert data["status"] == "COMPLETED", data.get("error")
        blob = next(iter(uploaded.values())).decode("latin-1")
        assert [r for r, _i, _n in rp.pdb_ca_sequence(blob)["A"]] == list(range(11, 31))
        assert all(c["target_numbering"] == "input" for c in data["candidates"])
        assert "A25" in _keys(blob)
        assert not _duplicate_residue_ids(blob), _duplicate_residue_ids(blob)

    def test_the_benign_heteroatom_shard_renders_the_reassuring_banner(
            self, tmp_path, monkeypatch):
        """...and the operator is not warned about a file that is fine. This is
        the half a pipeline assertion cannot see: the cost of over-refusing is
        a sentence on the results page, not a field in a payload."""
        data, _uploaded = self._drive(tmp_path, monkeypatch,
                                      job_spec=self._custom(),
                                      design_text=self._with_heteroatom(9000))
        html = _render_results(data["candidates"])
        assert "residue numbers from the file you uploaded" in html
        assert "will not resolve" not in html

    def test_a_colliding_heteroatom_still_costs_the_shard_its_numbering(
            self, tmp_path, monkeypatch):
        """The other side, which must NOT be relaxed. ``A25`` is inside the
        crop's 11-30, so design residue 15 is being renumbered onto the id the
        zinc already holds; delivering that file would put two different
        residues on ``A25``. The shard ships in 1..N and says so."""
        data, uploaded = self._drive(tmp_path, monkeypatch,
                                     job_spec=self._custom(),
                                     design_text=self._with_heteroatom(25))
        assert data["status"] == "COMPLETED", data.get("error")
        assert all(c["target_numbering"] == "upstream" for c in data["candidates"])
        assert next(iter(uploaded.values())) == self._with_heteroatom(25).encode(
            "latin-1")
        html = _render_results(data["candidates"])
        assert "will not resolve" in html

    # -- the design that was already in the operator's numbering ------------

    def _already_correct(self, tmp_path, monkeypatch):
        """A run whose crop and whose design carry the SAME residue numbers.

        Contig ``A1-20,B1-20`` crops the 40-residue upload to 1..20, which is
        exactly what upstream renumbers the design's target chains to. Nothing
        needs rewriting and nothing is rewritten — but the operator's numbering
        is what the delivered file carries, so the payload must say ``input``.
        """
        return self._drive(
            tmp_path, monkeypatch,
            job_spec=self._custom(target_input="A1-20,B1-20"),
            design_text=self._design_text(seq_a=_SEQ_A, seq_b=_SEQ_B))

    def test_a_design_already_in_the_operators_numbering_reports_input(
            self, tmp_path, monkeypatch):
        """F3. ``elif rep["already_input_numbering"]:`` -> ``elif False:`` left
        the whole file green, and this is not an exotic input: any target
        already numbered from 1 lands here. An AlphaFold or ESMFold model is
        ALWAYS 1..N, so upstream's numbering already equals the operator's.

        Break this branch and exactly those operators are told their file
        carries the design tool's numbering and their hotspot labels will not
        resolve — while holding a file in which they do.
        """
        data, uploaded = self._already_correct(tmp_path, monkeypatch)
        assert data["status"] == "COMPLETED", data.get("error")
        assert uploaded
        assert [c["target_numbering"] for c in data["candidates"]] == ["input"] * 2
        assert [d["target_numbering"] for d in data["designs"]] == ["input"] * 2

    def test_the_already_correct_design_is_shipped_byte_for_byte(
            self, tmp_path, monkeypatch):
        """The other half, and what tells this path apart from the applied one:
        "already correct" must mean UNTOUCHED, not "rewritten to the same
        numbers". A rewrite that happened to land on the same ids would still
        have re-spliced every coordinate record."""
        _data, uploaded = self._already_correct(tmp_path, monkeypatch)
        expected = self._design_text(seq_a=_SEQ_A, seq_b=_SEQ_B).encode("latin-1")
        for name, blob in uploaded.items():
            assert blob == expected, name

    def test_the_already_correct_design_renders_the_reassuring_banner(
            self, tmp_path, monkeypatch):
        """...and the operator is told so. This is the sentence the branch
        exists to earn."""
        data, _uploaded = self._already_correct(tmp_path, monkeypatch)
        html = _render_results(data["candidates"])
        assert "residue numbers from the file you uploaded" in html
        assert "will not resolve" not in html

    # -- curated runs -------------------------------------------------------

    def _curated(self, tmp_path, monkeypatch):
        """A curated benchmark run, with a staged file PLANTED under the curated
        task name that would map cleanly if anything ever read it."""
        return self._drive(
            tmp_path, monkeypatch,
            job_spec={"config_name": "search_binder_local_pipeline",
                      "task_name": "02_PDL1", "rf3_required": False,
                      "nsamples": 4, "replicas": 2},
            plant_staged="02_PDL1")

    def test_a_curated_run_is_never_renumbered(self, tmp_path, monkeypatch):
        """Kills "run the restore on curated runs too".

        A curated run designs against a bundled benchmark target that this
        wrapper never staged, so there is no operator numbering to restore and
        nothing may be rewritten. The mutation is inert unless a file happens to
        exist at the staged path for the curated task name -- so this test
        PLANTS one that would map cleanly. Correct code never looks at it.

        F1: the recorded value is ``n/a``, not ``upstream``. Both describe a
        file in 1..N, but they are answers to different questions.
        ``upstream`` means "you gave us a numbering and we could not restore
        it", and the results page says so in those words. On a curated run
        there was no uploaded file and no numbering to restore, so that
        sentence is false about an entire class of runs.
        """
        data, uploaded = self._curated(tmp_path, monkeypatch)
        assert data["status"] == "COMPLETED", data.get("error")
        assert uploaded
        for blob in uploaded.values():
            assert blob == self._design_text().encode("latin-1")
        assert all(c["target_numbering"] == "n/a" for c in data["candidates"])
        assert all(d["target_numbering"] == "n/a" for d in data["designs"])

    def test_a_curated_shard_renders_no_numbering_banner_at_all(
            self, tmp_path, monkeypatch):
        """F1, as the operator meets it: the REAL candidates a curated run
        produces, through the REAL results partial.

        Neither half can catch this alone. The pipeline test asserts a string
        without knowing what the page does with it, and a template test asserts
        whatever value the test itself passes in. Composing them is what
        showed that every curated run was rendering "not the numbering in the
        file you uploaded" to an operator who uploaded nothing.
        """
        data, _uploaded = self._curated(tmp_path, monkeypatch)
        html = _render_results(data["candidates"])
        assert "Residue numbering" not in html, html
        assert "will not resolve" not in html
        assert "file you uploaded" not in html

    def test_a_custom_run_that_could_not_be_restored_still_says_upstream(
            self, tmp_path, monkeypatch):
        """The boundary the new third value must not swallow. When the operator
        DID upload a file and the restore declined, the warning is true and has
        to keep firing — silence there would be the opposite defect."""
        scrambled = self._design_text(
            seq_a=list(reversed(self._cropped_names(_SEQ_A))))
        data, _uploaded = self._drive(tmp_path, monkeypatch,
                                      job_spec=self._custom(),
                                      design_text=scrambled)
        assert all(c["target_numbering"] == "upstream" for c in data["candidates"])
        html = _render_results(data["candidates"])
        assert "will not resolve" in html

    # -- the paid-design guarantee -----------------------------------------

    def test_a_numbering_failure_never_costs_a_paid_design(
            self, tmp_path, monkeypatch):
        """B8. The restore used to sit INSIDE the upload's ``try``, whose
        ``except Exception`` increments ``n_failures`` and DROPS the design --
        while a comment two lines above claimed it was outside the upload's
        failure accounting. ``restore_design_numbering`` never raises, but the
        decode/encode around it were outside that guarantee, so this makes the
        restore itself raise and asserts every design is still delivered."""
        def boom(*_a, **_k):
            raise RuntimeError("numbering exploded")
        monkeypatch.setattr(rp, "restore_design_numbering", boom)
        data, uploaded = self._drive(tmp_path, monkeypatch, job_spec=self._custom())
        assert data["status"] == "COMPLETED", data.get("error")
        assert data["n_failures"] == 0
        assert data["designs_completed"] == 2
        assert set(uploaded) == {"design_001.pdb", "design_002.pdb"}
        assert all(c["target_numbering"] == "upstream" for c in data["candidates"])

    def test_an_unreadable_staged_target_never_kills_the_shard(
            self, tmp_path, monkeypatch):
        """B7. The staged-target read was guarded by ``except OSError`` only,
        and it sits inside main()'s outer ``try:``/``finally:`` -- which has NO
        ``except``. A non-OSError there kills a shard that has already paid for
        its GPU and loses all 8 designs. Nothing about a residue number is worth
        that, so the guard has to catch everything."""
        real = rp.pdb_ca_sequence
        calls = {"n": 0}

        def sometimes_explodes(text):
            calls["n"] += 1
            if calls["n"] == 1:          # the staged-reference parse
                raise ValueError("not an OSError")
            return real(text)
        monkeypatch.setattr(rp, "pdb_ca_sequence", sometimes_explodes)
        data, uploaded = self._drive(tmp_path, monkeypatch, job_spec=self._custom())
        assert data["status"] == "COMPLETED", data.get("error")
        assert data["designs_completed"] == 2
        assert set(uploaded) == {"design_001.pdb", "design_002.pdb"}
        assert all(c["target_numbering"] == "upstream" for c in data["candidates"])


def test_the_upload_loop_restores_numbering_and_records_which_it_shipped():
    """The pure functions above are worthless if run() never calls them.

    A STRUCTURAL SMOKE CHECK, NOT THE CALL SITE'S TEST. This assertion is what
    the call site used to have INSTEAD of a test, and reviewer B killed six
    separate mutations of the upload loop -- deleting the renumber block
    outright among them -- without moving it. ``TestUploadLoopNumbering`` below
    is the real coverage; this survives only because "the name appears in a
    Call node" is cheap and catches a whole-function deletion early.
    """
    src = (_PROTEINA_DIR / "run_pipeline.py").read_text(encoding="utf-8")
    called = {n.func.id for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "restore_design_numbering" in called, (
        "run_pipeline defines the restore but never calls it -- the delivered "
        "design would still carry upstream's 1..N numbering")
    assert "pdb_ca_sequence" in called
    assert '"target_numbering"' in src, (
        "the result must record which numbering the delivered file carries")
