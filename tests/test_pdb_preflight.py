"""Tests for shared.pdb_preflight — per-tool hard gate + AF suggestion.

Synthetic + real-PDB fixtures. The real PDBs (3IUT, 3KKU, AF-P24807)
live under tools-hub/tmp/pdb_compare/ from the rfantibody investigation;
each test that needs them skips gracefully if the file is missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shared.pdb_preflight import (
    BINDER_DESIGN_TOOLS,
    BOLTZ2_COMPLEX_HARD_CAP_AA,
    HOTSPOTS_REQUIRED,
    MIN_TARGET_RESIDUES,
    PREFLIGHT_TOOLS,
    PreflightVerdict,
    VerdictKind,
    preflight_for_tool,
)

# ---------------------------------------------------------------------------
# Real-PDB fixtures (set by the rfantibody investigation; optional).
# ---------------------------------------------------------------------------

PDB_DIR = Path(__file__).resolve().parents[1] / "tmp" / "pdb_compare"

HCRUZ_3IUT = PDB_DIR / "hcruz_3iutclean.pdb"
HCRUZ_3KKU = PDB_DIR / "hcruz_3kku.pdb"
LEDOGEN_AF = PDB_DIR / "ledogen_AF-P24807-F1-model_v6 (1).pdb"


def _require(p: Path) -> bytes:
    if not p.exists():
        pytest.skip(f"missing fixture: {p}")
    return p.read_bytes()


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

CLEAN_FOUR_RES_PDB = b"""\
HEADER    CLEAN
ATOM      1  N   ALA A  10       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A  10       2.000   1.000   1.000  1.00 10.00           C
ATOM      3  C   ALA A  10       3.000   1.000   1.000  1.00 10.00           C
ATOM      4  O   ALA A  10       3.000   2.000   1.000  1.00 10.00           O
ATOM      5  N   GLY A  11       4.000   1.000   1.000  1.00 10.00           N
ATOM      6  CA  GLY A  11       5.000   1.000   1.000  1.00 10.00           C
ATOM      7  C   GLY A  11       6.000   1.000   1.000  1.00 10.00           C
ATOM      8  O   GLY A  11       6.000   2.000   1.000  1.00 10.00           O
END
"""


# ---------------------------------------------------------------------------
# Sanity: registration
# ---------------------------------------------------------------------------

def test_binder_design_tools_locked():
    # pxdesign joined the binder-design gate (gap 1): it takes a target +
    # required hotspots and runs the same normalizer dry-run + structural
    # checks.
    #
    # proteina joined when bring-your-own targets landed. It was absent while
    # only curated ~115 aa benchmark targets were reachable, which meant it got
    # NO size check at all — harmless then, a real cost hole once users can
    # upload their own: an oversized target runs to the 7200 s wall, is killed,
    # and bills the full per-shard ceiling for zero designs across every shard
    # in the wave. Its SizeEnvelope is the gate that stops that.
    assert BINDER_DESIGN_TOOLS == frozenset({
        "rfantibody", "rfdiffusion", "bindcraft", "boltzgen", "pxdesign",
        "proteina",
    })


def test_every_binder_design_tool_has_a_preview_fn():
    """preflight_for_tool indexes _PREVIEW_FN AFTER its BINDER_DESIGN_TOOLS
    membership check and OUTSIDE the try block, so a tool added to TOOL_RULES
    without a normalizer raises KeyError out of a function documented never to
    raise. That 500s the AJAX preflight route and makes the submit-time size
    gate silently no-op — i.e. it disables the cost guard while looking fine.
    proteina shipped in exactly that state."""
    from shared.pdb_preflight import _PREVIEW_FN

    assert set(_PREVIEW_FN) == BINDER_DESIGN_TOOLS


def test_preflight_for_proteina_returns_a_verdict():
    """Before the _PREVIEW_FN entry existed this raised KeyError('proteina')."""
    from shared.pdb_preflight import preflight_for_tool

    verdict = preflight_for_tool("proteina", CLEAN_FOUR_RES_PDB,
                                 target_chain="A", hotspots=[])
    assert verdict is not None
    assert verdict.tool_slug == "proteina"


def test_proteina_rules_allow_multi_chain_and_optional_hotspots():
    """Proteina is hotspot-DIRECTED but not hotspot-REQUIRED (an open search is
    a legitimate run), and a three-chain target is a validated upstream case."""
    from shared.pdb_preflight_rules import TOOL_RULES
    rules = TOOL_RULES["proteina"]
    assert rules.multi_chain_supported is True
    assert rules.hotspots_required is False
    assert "proteina" not in HOTSPOTS_REQUIRED


def test_preflight_tools_includes_boltz2():
    # boltz2 gets a hard-gate too, but via its own evaluator, so it is not
    # a "binder design tool".
    assert PREFLIGHT_TOOLS == BINDER_DESIGN_TOOLS | frozenset({"boltz2"})
    assert "boltz2" not in BINDER_DESIGN_TOOLS


def test_hotspots_required_for_four_tools():
    assert HOTSPOTS_REQUIRED == frozenset({
        "rfantibody", "rfdiffusion", "bindcraft", "pxdesign",
    })


# ---------------------------------------------------------------------------
# Hard-gate cases
# ---------------------------------------------------------------------------

def test_missing_chain_blocks_with_did_you_mean():
    """User typed chain B but PDB only has chain A → reject + suggest A."""
    v = preflight_for_tool(
        "rfantibody",
        CLEAN_FOUR_RES_PDB,
        target_chain="B",
        hotspots=[10, 11],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert not v.ok
    assert "B" in v.reason
    assert "A" in v.suggested_fix


def test_too_few_residues_blocks():
    """A two-residue chain is below MIN_TARGET_RESIDUES → reject."""
    v = preflight_for_tool(
        "rfantibody",
        CLEAN_FOUR_RES_PDB,
        target_chain="A",
        hotspots=[10],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert str(MIN_TARGET_RESIDUES) in v.reason


def test_missing_hotspot_required_blocks():
    """rfantibody requires hotspots; empty list → reject."""
    data = _require(LEDOGEN_AF)  # 76 residues, clean
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert "hotspot" in v.reason.lower()


def test_boltzgen_allows_empty_hotspots():
    """boltzgen's hotspot list is optional — empty must not block."""
    data = _require(LEDOGEN_AF)
    v = preflight_for_tool(
        "boltzgen", data, target_chain="A", hotspots=[],
    )
    assert v.ok
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK)


def test_dropped_hotspot_blocks_with_nearest_suggestions():
    """When a user hotspot has no clean backbone, return nearest clean residues."""
    data = _require(LEDOGEN_AF)
    # Residue 999 is way out of range — backbone is "missing" in the
    # sense that there's no atom record at all. The verdict should be
    # NEEDS_FIX. nearest_clean_residues may be empty for out-of-range
    # picks (window=10 misses the real residue range).
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[999],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert 999 in v.hotspot_status["dropped"]


# ---------------------------------------------------------------------------
# Ready cases — the rfantibody hcruz fixtures
# ---------------------------------------------------------------------------

def test_3iut_ready_with_af_fallback():
    """3IUT has 74 altloc records — gate should pass after collapse + offer AF."""
    data = _require(HCRUZ_3IUT)
    v = preflight_for_tool(
        "rfantibody",
        data,
        target_chain="A",
        hotspots=[181, 182, 183, 184, 188],
    )
    assert v.ok
    assert v.kind is VerdictKind.READY_WITH_FALLBACK
    assert v.alphafold is not None
    assert v.alphafold.uniprot_accession == "P25779"
    # All five hotspots survive cleanup.
    assert v.hotspot_status["surviving"] == [181, 182, 183, 184, 188]
    assert v.hotspot_status["dropped"] == []
    # Cleanup summary mentions altloc collapse.
    assert any("alternate conformation" in item for item in v.cleanup.items)


def test_3kku_ready_with_af_fallback():
    """3KKU — same UniProt as 3IUT, 65 altloc records."""
    data = _require(HCRUZ_3KKU)
    v = preflight_for_tool(
        "rfantibody",
        data,
        target_chain="A",
        hotspots=[182, 183, 184],
    )
    assert v.ok
    assert v.kind is VerdictKind.READY_WITH_FALLBACK
    assert v.alphafold.uniprot_accession == "P25779"


def test_af_input_is_ready_without_fallback_suggestion():
    """AlphaFold model with no altloc and no dropped residues — plain READY."""
    data = _require(LEDOGEN_AF)
    v = preflight_for_tool(
        "rfantibody",
        data,
        target_chain="A",
        hotspots=[12, 24, 45],
    )
    assert v.ok
    # Already an AF input — no cleanup needed, so no "use AF" suggestion.
    assert v.kind is VerdictKind.READY
    assert v.cleanup.items == []
    assert v.cleanup.altloc_records_collapsed == 0


# ---------------------------------------------------------------------------
# Non-binder tools fall through to a no-op READY
# ---------------------------------------------------------------------------

def test_non_binder_tool_falls_through():
    """A tool not in BINDER_DESIGN_TOOLS shouldn't gate at all."""
    v = preflight_for_tool(
        "mpnn", CLEAN_FOUR_RES_PDB, target_chain="A", hotspots=[10],
    )
    assert v.kind is VerdictKind.READY
    assert v.ok


# ---------------------------------------------------------------------------
# AlphaFold suggestion when no UniProt mapping is present
# ---------------------------------------------------------------------------

def test_no_alphafold_when_no_dbref():
    """Synthetic PDB has no DBREF — no AF suggestion offered."""
    v = preflight_for_tool(
        "rfantibody",
        CLEAN_FOUR_RES_PDB,
        target_chain="A",
        hotspots=[10, 11],
    )
    # Verdict will be NEEDS_FIX (too few residues), but alphafold field
    # should be None regardless.
    assert v.alphafold is None


# ===========================================================================
# v2: TOOL_RULES module sanity + gap detection + size envelope
# ===========================================================================

from shared.pdb_preflight_rules import (   # noqa: E402
    TOOL_RULES, runtime_estimate_min,
)


def _atom_line(
    serial: int, name: str, resname: str, chain: str,
    resnum: int, x: float, y: float, z: float,
    *, occ: float = 1.00, bfac: float = 10.00, icode: str = " ",
) -> str:
    """Build a column-perfect PDB ATOM record."""
    elem = name[0].rjust(2)
    aname = f" {name:<3s}" if len(name) < 4 else name[:4]
    return (
        f"ATOM  {serial:5d} {aname} {resname:3s} "
        f"{chain:1s}{resnum:4d}{icode:1s}   "
        f"{x:8.3f}{y:8.3f}{z:8.3f}"
        f"{occ:6.2f}{bfac:6.2f}          {elem}\n"
    )


def _chain_pdb(chain_id: str, residues, *, header: str = "SYNTHETIC") -> bytes:
    """Build a synthetic PDB with N/CA/C/O backbone for each residue.

    ``residues`` is a list of int resnums (icode defaulted to ' '), or
    a list of (resnum, icode) tuples for antibody-style insertions.
    Coordinates are spread along x so consecutive resnums never coincide.
    """
    lines = [f"HEADER    {header}\n"]
    serial = 0
    for i, r in enumerate(residues):
        if isinstance(r, tuple):
            rn, icode = r
        else:
            rn, icode = r, " "
        x_base = float(i * 4.0)
        for atom_name, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
            serial += 1
            y = 1.0 if atom_name != "O" else 2.0
            lines.append(_atom_line(
                serial=serial, name=atom_name, resname="ALA",
                chain=chain_id, resnum=rn,
                x=x_base + off, y=y, z=1.0, icode=icode,
            ))
    lines.append("END\n")
    return "".join(lines).encode()


# ---------------------------------------------------------------------------
# TOOL_RULES sanity
# ---------------------------------------------------------------------------

def test_tool_rules_cover_all_binder_tools():
    """Every binder tool has a complete ToolRules entry."""
    assert set(TOOL_RULES.keys()) == BINDER_DESIGN_TOOLS


def test_tool_rules_invariants():
    """soft_warn < hard_cap, combined_cap >= hard_cap for every tool."""
    for slug, rules in TOOL_RULES.items():
        assert rules.size.soft_warn_target_aa < rules.size.hard_cap_target_aa, slug
        assert rules.size.hard_cap_combined_aa >= rules.size.hard_cap_target_aa, slug
        assert rules.min_target_aa > 0, slug
        assert rules.size.runtime_base_min > 0, slug
        assert rules.size.runtime_alpha > 0, slug


def test_runtime_estimate_floor():
    """Runtime estimate floors at 5 minutes for tiny targets."""
    r = TOOL_RULES["rfantibody"]
    assert runtime_estimate_min(r, target_aa=10, num_designs=1) >= 5.0


def test_runtime_estimate_scales_with_designs():
    """Doubling design count roughly doubles runtime estimate."""
    r = TOOL_RULES["rfdiffusion"]
    base = runtime_estimate_min(r, target_aa=120, num_designs=100)
    bigger = runtime_estimate_min(r, target_aa=120, num_designs=200)
    assert bigger > base * 1.8  # ~2x with a tolerance


def test_runtime_estimate_scales_with_target_size():
    """A bigger target should give a longer runtime estimate."""
    r = TOOL_RULES["rfdiffusion"]
    small = runtime_estimate_min(r, target_aa=100, num_designs=100)
    big = runtime_estimate_min(r, target_aa=400, num_designs=100)
    assert big > small * 2.0


# ---------------------------------------------------------------------------
# Internal gap detection
# ---------------------------------------------------------------------------

def test_no_gap_in_contiguous_chain():
    """Contiguous chain 50..120 → no gaps detected for any tool."""
    data = _chain_pdb("A", list(range(50, 121)))   # 71 residues
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[60, 70, 80],
    )
    assert v.gap_analysis is not None
    assert v.gap_analysis.gaps == []
    assert v.gap_analysis.longest_gap == 0
    assert not v.gap_analysis.causes_hard_fail


def test_end_of_chain_truncation_is_not_a_gap():
    """Chain starting at 19 (missing 1-18) is NOT a gap — N-terminal trunc."""
    data = _chain_pdb("A", list(range(19, 70)))    # 51 residues, no internal gap
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[30, 40],
    )
    assert v.gap_analysis.gaps == []


def test_rfdiffusion_blocks_on_any_internal_gap():
    """rfdiffusion: any single internal gap → NEEDS_FIX (contig assert)."""
    # 1-30 then 35-100 → 4-residue gap at 31-34
    data = _chain_pdb("A", list(range(1, 31)) + list(range(35, 101)))
    v = preflight_for_tool(
        "rfdiffusion", data, target_chain="A", hotspots=[60, 70, 80],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.gap_analysis.causes_hard_fail
    assert v.gap_analysis.longest_gap == 4
    assert "contig" in v.reason.lower() or "assert" in v.reason.lower()


def test_rfantibody_gap_near_hotspot_hard_fails():
    """rfantibody: 3+ residue gap within 10 residues of a hotspot → NEEDS_FIX."""
    # 1-49 then 55-100 → 5-residue gap 50-54; hotspot 53 is ~0 from the gap
    data = _chain_pdb("A", list(range(1, 50)) + list(range(55, 101)))
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[60, 70],
    )
    # Hotspot 60 is 6 residues from gap end (54), within 10 → hard fail
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.gap_analysis.causes_hard_fail
    assert v.gap_analysis.longest_gap == 5


def test_rfantibody_gap_far_from_hotspot_warns_only():
    """rfantibody: 8-residue gap with hotspots 50+ residues away → WARN only."""
    # 1-30 then 39-200 → 8-residue gap 31-38; hotspots 100, 150 (>50 away)
    data = _chain_pdb("A", list(range(1, 31)) + list(range(39, 201)))
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[100, 150],
    )
    # Gap is length 8 ≥ warn_length 5; hotspots are >10 away → no hard fail
    assert v.gap_analysis.longest_gap == 8
    assert not v.gap_analysis.causes_hard_fail
    assert v.gap_analysis.warn_message is not None
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK)


def test_boltzgen_tolerates_small_gaps_silently():
    """boltzgen: 5-residue gap below warn_length 20 → no warn, no fail."""
    data = _chain_pdb("A", list(range(1, 50)) + list(range(55, 101)))
    v = preflight_for_tool(
        "boltzgen", data, target_chain="A", hotspots=[],
    )
    assert v.gap_analysis.longest_gap == 5
    assert not v.gap_analysis.causes_hard_fail
    assert v.gap_analysis.warn_message is None


def test_bindcraft_large_gap_near_hotspot_hard_fails():
    """bindcraft: gap ≥ 20 within 5 residues of hotspot → NEEDS_FIX."""
    # 1-50 then 75-150 → 24-residue gap 51-74; hotspot 78 is 3 from gap end
    data = _chain_pdb("A", list(range(1, 51)) + list(range(75, 151)))
    v = preflight_for_tool(
        "bindcraft", data, target_chain="A", hotspots=[78, 100],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.gap_analysis.causes_hard_fail


def test_antibody_icodes_do_not_create_false_gap():
    """Antibody CDR insertion codes (100A, 100B, 100C) ≠ gap.

    Uses a 40-residue chain (above MIN_TARGET_RESIDUES) with icode
    insertions embedded in the middle so we actually reach the gap
    analyser instead of short-circuiting on the min-residue check.
    """
    # 50..74 then 75 with icodes ' ','A','B','C' then 76..100 → 51 unique
    # integer resnums; contiguous in integer space; icodes are insertions
    # WITHIN resnum 75 and shouldn't appear as numbering gaps.
    residues: list = list(range(50, 75))
    residues += [(75, " "), (75, "A"), (75, "B"), (75, "C")]
    residues += list(range(76, 101))
    data = _chain_pdb("A", residues)
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[75, 80],
    )
    # No gap should be detected — integer resnums are 50-100 contiguous,
    # icode-distinguished 75A/B/C fold into the same integer resnum 75.
    assert v.gap_analysis is not None
    assert v.gap_analysis.gaps == []
    assert v.gap_analysis.longest_gap == 0


# ---------------------------------------------------------------------------
# Size envelope
# ---------------------------------------------------------------------------

def test_size_envelope_within_caps_emits_runtime_estimate():
    """100-aa target with 4 designs → runtime estimate populated, under caps."""
    data = _chain_pdb("A", list(range(1, 101)))
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[50],
        binder_max_aa=120, num_designs=4,
    )
    assert v.size_envelope is not None
    assert v.size_envelope.residue_count == 100
    assert v.size_envelope.runtime_estimate_min is not None
    assert v.size_envelope.runtime_estimate_min >= 5.0
    assert v.size_envelope.runtime_basis == "4 designs"
    assert not v.size_envelope.over_soft_warn
    assert not v.size_envelope.over_hard_cap


def test_size_envelope_omits_runtime_when_num_designs_missing():
    """Without num_designs, the panel shouldn't surface a runtime guess."""
    data = _chain_pdb("A", list(range(1, 101)))
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[50],
    )
    assert v.size_envelope is not None
    assert v.size_envelope.runtime_estimate_min is None


def test_size_soft_warn_surfaces_amber_message():
    """rfdiffusion at 350 aa (soft_warn=300, hard=500) → amber warn, still passes."""
    data = _chain_pdb("A", list(range(1, 351)))   # 350 aa
    v = preflight_for_tool(
        "rfdiffusion", data, target_chain="A", hotspots=[100, 200],
    )
    # 350 > soft_warn 300 but < hard_cap 500
    assert v.size_envelope.over_soft_warn
    assert not v.size_envelope.over_hard_cap
    assert v.size_envelope.warn_message is not None
    assert v.kind is VerdictKind.READY_WITH_FALLBACK or v.kind is VerdictKind.READY


def test_size_hard_cap_blocks_oversized_target():
    """rfdiffusion at 550 aa (cap=500) → NEEDS_FIX."""
    data = _chain_pdb("A", list(range(1, 551)))   # 550 aa
    v = preflight_for_tool(
        "rfdiffusion", data, target_chain="A", hotspots=[100, 200, 300],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.size_envelope.over_hard_cap
    assert "GPU envelope" in v.reason or "out of memory" in v.reason.lower()


def test_combined_budget_blocks_target_plus_binder():
    """rfdiffusion 450 aa target + 200 aa binder = 650 > 600 combined cap."""
    data = _chain_pdb("A", list(range(1, 451)))   # 450 aa
    v = preflight_for_tool(
        "rfdiffusion", data, target_chain="A", hotspots=[100, 200],
        binder_max_aa=200, num_designs=4,
    )
    # 450 < 500 (no hard cap on target alone)
    # But 450 + 200 = 650 > 600 combined cap
    assert v.kind is VerdictKind.NEEDS_FIX
    assert not v.size_envelope.over_hard_cap
    assert v.size_envelope.over_combined_cap
    assert "combined" in v.reason.lower() or str(v.size_envelope.combined_aa) in v.reason


def test_bindcraft_tighter_cap_blocks_what_rfdiffusion_allows():
    """bindcraft hard_cap=500 should block a 510-aa target rfdiffusion (cap=500 too) blocks."""
    # Now both have hard_cap=500. Use bindcraft soft_warn=300 vs rfdiffusion
    # soft_warn=300 (same). Verify bindcraft blocks at exactly its cap and
    # rfdiffusion blocks at its cap.
    data_510 = _chain_pdb("A", list(range(1, 511)))   # 510 aa
    v_bc = preflight_for_tool(
        "bindcraft", data_510, target_chain="A", hotspots=[100, 200],
    )
    assert v_bc.kind is VerdictKind.NEEDS_FIX
    assert v_bc.size_envelope.over_hard_cap

    # 510 also exceeds rfdiffusion's hard_cap (500)
    v_rf = preflight_for_tool(
        "rfdiffusion", data_510, target_chain="A", hotspots=[100, 200],
    )
    assert v_rf.size_envelope.over_hard_cap


def test_size_envelope_gpu_field_populated():
    """GPU label is plumbed through so the panel can display it.

    rfantibody runs on A100-40GB, the GPU configured for the deployed
    ranomics-rfantibody-prod app (llm-pd infrastructure/modal/
    rfantibody_app.py _GPU, corroborated by base_image.py and
    backend/pipelines/rfantibody.py).
    """
    data = _chain_pdb("A", list(range(1, 101)))
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[50],
    )
    assert v.size_envelope.gpu == "A100-40GB"
    v_bg = preflight_for_tool(
        "boltzgen", data, target_chain="A", hotspots=[],
    )
    assert v_bg.size_envelope.gpu == "A100-40GB"


# ---------------------------------------------------------------------------
# Runtime estimate (advisory only — the tier-collapse PR retired the
# wall-clock hard cap; the estimator now surfaces minutes as a panel hint
# but never blocks submit. The remaining test pins the estimator math to
# the existing curve so an accidental anchor change is caught.)
# ---------------------------------------------------------------------------

def test_runtime_estimate_scales_with_design_count():
    """200 designs takes 50x longer than 4 designs at the same target size."""
    data = _chain_pdb("A", list(range(1, 201)))   # 200 aa
    big = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[50],
        binder_max_aa=120, num_designs=200,
    )
    small = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[50],
        binder_max_aa=120, num_designs=4,
    )
    # Both should pass preflight on size grounds; runtime cap retired.
    assert big.kind is not VerdictKind.NEEDS_FIX
    assert small.kind is not VerdictKind.NEEDS_FIX
    # Estimate scales linearly in num_designs (200 / 4 = 50x).
    assert big.size_envelope.runtime_estimate_min is not None
    assert small.size_envelope.runtime_estimate_min is not None
    ratio = big.size_envelope.runtime_estimate_min / small.size_envelope.runtime_estimate_min
    assert 40.0 < ratio < 55.0  # allow floor + arithmetic slack


def test_runtime_estimate_omitted_without_num_designs():
    """No num_designs → estimator does not surface a misleading value."""
    data = _chain_pdb("A", list(range(1, 101)))
    v = preflight_for_tool(
        "rfantibody", data, target_chain="A", hotspots=[50],
    )
    assert v.size_envelope.runtime_estimate_min is None


# ---------------------------------------------------------------------------
# pxdesign hard-gate (gap 1) — original-PDB-numbering hotspots, same machinery
# as the other binder tools.
# ---------------------------------------------------------------------------

def test_pxdesign_missing_chain_blocks():
    v = preflight_for_tool(
        "pxdesign", CLEAN_FOUR_RES_PDB, target_chain="Z", hotspots=[10],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert "Z" in v.reason


def test_pxdesign_requires_hotspots():
    data = _chain_pdb("A", list(range(1, 101)))   # 100 aa, clean
    v = preflight_for_tool(
        "pxdesign", data, target_chain="A", hotspots=[],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert "hotspot" in v.reason.lower()


def test_pxdesign_oversized_target_blocks():
    """pxdesign hard_cap_target_aa=600 → a 650-aa target is rejected upfront."""
    data = _chain_pdb("A", list(range(1, 651)))   # 650 aa
    v = preflight_for_tool(
        "pxdesign", data, target_chain="A", hotspots=[100, 200],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.size_envelope.over_hard_cap
    assert "GPU envelope" in v.reason or "out of memory" in v.reason.lower()


def test_pxdesign_internal_gap_near_hotspot_blocks():
    """A sizeable internal gap within reach of a hotspot hard-fails."""
    # gap 51..64 (14 residues) adjacent to hotspot 50.
    data = _chain_pdb("A", list(range(1, 51)) + list(range(65, 131)))
    v = preflight_for_tool(
        "pxdesign", data, target_chain="A", hotspots=[50],
    )
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.gap_analysis is not None and v.gap_analysis.causes_hard_fail


def test_pxdesign_clean_target_ready():
    data = _chain_pdb("A", list(range(1, 121)))   # 120 aa, contiguous
    v = preflight_for_tool(
        "pxdesign", data, target_chain="A", hotspots=[40, 60, 80],
        binder_max_aa=120, num_designs=8,
    )
    assert v.ok
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK)


# ---------------------------------------------------------------------------
# boltz2 dedicated preflight (gap 1) — 1-indexed SEQUENCE-position hotspots,
# optional hotspots, single antigen chain.
# ---------------------------------------------------------------------------

def test_boltz2_clean_antigen_ready():
    data = _chain_pdb("A", list(range(1, 121)))   # 120 residues
    v = preflight_for_tool(
        "boltz2", data, target_chain="A", hotspots=[10, 55, 120],
    )
    assert v.ok
    assert v.kind is VerdictKind.READY


def test_boltz2_empty_hotspots_ok():
    """boltz2 hotspots are optional."""
    data = _chain_pdb("A", list(range(1, 121)))
    v = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[])
    assert v.ok


def test_boltz2_hotspot_uses_sequence_position_not_resnum():
    """The defining boltz2 distinction: a 101-residue antigen numbered
    20..120 accepts hotspot position 5 (the 5th residue) even though
    resnum 5 does not exist, and rejects position 200."""
    data = _chain_pdb("A", list(range(20, 121)))   # 101 residues, numbered 20..120
    ok = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[5])
    assert ok.kind is VerdictKind.READY      # 5 <= 101, valid position
    bad = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[200])
    assert bad.kind is VerdictKind.NEEDS_FIX
    assert "200" in bad.reason
    assert "sequence position" in bad.suggested_fix.lower()
    assert "101" in bad.suggested_fix  # names the valid upper bound


def test_boltz2_missing_chain_blocks():
    data = _chain_pdb("A", list(range(1, 121)))
    v = preflight_for_tool("boltz2", data, target_chain="Q", hotspots=[5])
    assert v.kind is VerdictKind.NEEDS_FIX
    assert "Q" in v.reason
    # Names the chain that IS present so the user can correct.
    assert "A" in v.suggested_fix


def test_boltz2_oversized_antigen_blocks():
    over = BOLTZ2_COMPLEX_HARD_CAP_AA + 1
    data = _chain_pdb("A", list(range(1, over + 1)))   # cap + 1 residues
    v = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[])
    assert v.kind is VerdictKind.NEEDS_FIX
    assert "trim" in v.suggested_fix.lower()
    assert str(BOLTZ2_COMPLEX_HARD_CAP_AA) in v.reason


def test_boltz2_size_cap_counts_total_complex():
    """The cap is on antigen + longest binder, not the antigen alone."""
    # Antigen just under the cap on its own.
    n = BOLTZ2_COMPLEX_HARD_CAP_AA - 200
    data = _chain_pdb("A", list(range(1, n + 1)))
    # Antigen alone passes.
    ok = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[])
    assert ok.kind is VerdictKind.READY
    # Same antigen + a 400-aa binder pushes the complex over the cap.
    over = preflight_for_tool(
        "boltz2", data, target_chain="A", hotspots=[], binder_max_aa=400,
    )
    assert over.kind is VerdictKind.NEEDS_FIX
    assert str(n + 400) in over.reason          # names the total
    assert "binder" in over.reason.lower()


# ---------------------------------------------------------------------------
# boltz2 multi-chain antigen (gap 4) — run_pipeline folds only the named
# chain; the rest are silently dropped. Flag it upfront.
# ---------------------------------------------------------------------------

def _two_chain_pdb(chain_a_res, chain_b_res, *, b_chain="B") -> bytes:
    """Merge two single-chain bodies into one multi-chain PDB."""
    a_body = [
        ln for ln in _chain_pdb("A", chain_a_res).decode().splitlines()
        if ln.startswith("ATOM")
    ]
    b_body = [
        ln for ln in _chain_pdb(b_chain, chain_b_res).decode().splitlines()
        if ln.startswith("ATOM")
    ]
    return (
        "HEADER    TWOCHAIN\n" + "\n".join(a_body + b_body) + "\nEND\n"
    ).encode()


def test_boltz2_multi_chain_antigen_blocks():
    data = _two_chain_pdb(list(range(1, 101)), list(range(1, 81)))  # A=100, B=80
    v = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[10])
    assert v.kind is VerdictKind.NEEDS_FIX
    # Names both chains and which one is folded.
    assert "A" in v.reason and "B" in v.reason
    assert "only" in v.reason.lower()
    assert "B" in v.reason  # the dropped chain is named
    assert v.target_chain == "A"


def test_boltz2_multi_chain_names_chain_to_keep():
    data = _two_chain_pdb(list(range(1, 101)), list(range(1, 81)))
    v = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[])
    assert "just chain A" in v.suggested_fix


def test_boltz2_tiny_second_chain_not_blocked():
    """A 1-residue incidental chain must not trigger the multi-chain block."""
    data = _two_chain_pdb(list(range(1, 101)), [500])  # A=100, B=1 residue
    v = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[10])
    assert v.ok
    assert v.kind is VerdictKind.READY


def test_boltz2_single_chain_still_ready():
    """Regression: single-chain antigen is unaffected by the multi-chain gate."""
    data = _chain_pdb("A", list(range(1, 121)))
    v = preflight_for_tool("boltz2", data, target_chain="A", hotspots=[5, 60])
    assert v.kind is VerdictKind.READY


# ---------------------------------------------------------------------------
# Multi-chain targets
#
# `target_chain` may name several chains, whitespace-separated ("A B C"):
# shared/pdb_inspect.validate_target_chain has always accepted that form and
# 5 of the 6 registered tools declare multi_chain_supported=True (all but
# rfantibody). Every consumer in pdb_preflight compared the whole string
# against a one-letter chain id, so a multi-token value matched nothing —
# residues_kept_per_chain.get("A B C") returned 0 and a perfectly good target
# was rejected with "has only 0 protein residue(s)". Same defect class as the
# one fixed in validate_hotspots (A18) and pipeline_normalize.
#
# These cases all use proteina, the one tool that is multi_chain_container_ready
# and therefore the only one that reaches this code with several chains.
# ---------------------------------------------------------------------------

def _multi_chain_pdb(spec: dict, *, header: str = "SYNTHETIC") -> bytes:
    """``{chain_id: [resnums]}`` -> one PDB with a full backbone per residue."""
    body = b"".join(
        _chain_pdb(chain, residues, header=header)
        .replace(f"HEADER    {header}\n".encode(), b"")
        .replace(b"END\n", b"")
        for chain, residues in spec.items()
    )
    return f"HEADER    {header}\n".encode() + body + b"END\n"


def test_chain_tokens_splits_and_dedups():
    from shared.pdb_preflight import _chain_tokens
    assert _chain_tokens("A") == ["A"]          # unchanged for the common case
    assert _chain_tokens("A B C") == ["A", "B", "C"]
    assert _chain_tokens("  A   B  ") == ["A", "B"]
    assert _chain_tokens("A A B") == ["A", "B"]
    assert _chain_tokens("") == []
    assert _chain_tokens(None) == []


def test_multi_chain_target_is_not_rejected_as_empty():
    """The headline regression: three 40-residue chains is a 120-residue
    target, not a 0-residue one."""
    data = _multi_chain_pdb({
        "A": list(range(1, 41)),
        "B": list(range(1, 41)),
        "C": list(range(1, 41)),
    })
    v = preflight_for_tool("proteina", data, target_chain="A B C", hotspots=[])
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK), (
        v.kind, v.reason
    )
    assert "0 protein residue" not in (v.reason or "")


def test_multi_chain_residue_count_is_the_sum_across_chains():
    """min_target_aa is a property of the whole target. Three 15-residue chains
    clear proteina's floor of 30; any one of them alone would not."""
    data = _multi_chain_pdb({
        "A": list(range(1, 16)),
        "B": list(range(1, 16)),
        "C": list(range(1, 16)),
    })
    assert preflight_for_tool(
        "proteina", data, target_chain="A", hotspots=[],
    ).kind is VerdictKind.NEEDS_FIX
    v = preflight_for_tool("proteina", data, target_chain="A B C", hotspots=[])
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK)


def test_gaps_are_found_per_chain_not_across_the_seam():
    """Merging chains before gap detection invents a gap at the seam. Chain A
    is 1-20 and chain B is 100-120; each is internally contiguous, but a merged
    resnum list reads as one chain with a 79-residue hole."""
    data = _multi_chain_pdb({
        "A": list(range(1, 21)),
        "B": list(range(100, 121)),
    })
    v = preflight_for_tool("proteina", data, target_chain="A B", hotspots=[])
    assert v.gap_analysis.gaps == []
    assert v.gap_analysis.warn_message is None
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK)


def test_gap_message_names_the_offending_chain_not_the_whole_field():
    """A user given "Chain A B has a 30-residue gap" cannot act on it."""
    data = _multi_chain_pdb({
        "A": list(range(1, 41)),
        "B": list(range(1, 21)) + list(range(51, 71)),   # 30-residue hole
    })
    v = preflight_for_tool("proteina", data, target_chain="A B", hotspots=[])
    assert v.gap_analysis.longest_gap == 30
    assert "Chain B has" in (v.gap_analysis.warn_message or "")


def test_single_chain_gap_message_is_unchanged():
    """Guard for the other five tools: one chain in, identical text out."""
    data = _chain_pdb("A", list(range(1, 21)) + list(range(51, 91)))
    v = preflight_for_tool("proteina", data, target_chain="A", hotspots=[])
    assert "Chain A has a 30-residue internal gap" in (
        v.gap_analysis.warn_message or ""
    )


# ---------------------------------------------------------------------------
# Multi-chain is gated on MODEL capability AND CONTAINER capability
#
# Before the multi-chain work, a multi-chain target was refused for all six
# tools — not by any rule, but by a bug: every consumer in pdb_preflight /
# pipeline_normalize compared the whole `target_chain` string against a
# one-letter chain id, so "A B" matched no chain, normalize_for_pipeline
# dropped them all and raised, and preflight reported NEEDS_FIX ("Target chain
# 'A B' isn't in this PDB. Found chain(s): A, B." — wrong prose, right
# outcome). multi_chain_supported had no reader at all.
#
# Teaching THIS repo's consumers to split on whitespace was correct but only
# half the job. The images that run the job are built from the sibling repo
# llm-proteinDesigner, whose backend/pdb_utils/pipeline_normalize.py still
# exact-matches the chain and raises on "A B" — verified by executing it
# against a clean two-chain PDB for rfdiffusion / boltzgen / pxdesign /
# rfantibody. bindcraft ships from a separate prebuilt image that could not be
# inspected, so it is gated as unverified. So the fix did not grant a
# capability to the 4 tools that declare multi_chain_supported=True and are
# gated here; it removed a free refusal and replaced it with a funded run that
# dies in the container. 5 tools are blocked in total — those 4 plus
# rfantibody, whose model cannot do it either.
#
# Only proteina is genuinely ready: its container lives in this repo and was
# proven on a live A100. Hence two flags, and a gate that needs both.
# ---------------------------------------------------------------------------

# Tools whose container is NOT multi-chain ready today. Deliberately spelled
# out rather than derived from TOOL_RULES: if someone flips a flag, these
# tests must FAIL and make them justify it, not silently follow along.
_SINGLE_CHAIN_TOOLS = ["rfantibody", "rfdiffusion", "bindcraft",
                       "boltzgen", "pxdesign"]
_MULTI_CHAIN_TOOLS = ["proteina"]


def _two_chain_target() -> bytes:
    """Two full, healthy 120-residue chains. Big enough to clear every tool's
    min_target_aa and small enough to clear every hard cap, so the only thing
    a verdict can be reacting to is the chain COUNT."""
    return _multi_chain_pdb({
        "A": list(range(1, 121)),
        "B": list(range(1, 121)),
    })


@pytest.mark.parametrize("slug", _SINGLE_CHAIN_TOOLS)
def test_single_chain_tools_refuse_a_multi_chain_target(slug):
    """The regression guard, for every tool whose container can't do it."""
    v = preflight_for_tool(
        slug, _two_chain_target(), target_chain="A B", hotspots=[40, 60],
    )
    assert v.kind is VerdictKind.NEEDS_FIX, (slug, v.kind, v.reason)
    assert not v.ok
    # The refusal must name the real problem so the user can act on it. The
    # pre-existing accidental refusal said the chain "isn't in this PDB",
    # which is false and unactionable when both chains plainly are.
    assert "A" in (v.suggested_fix or "") and "B" in (v.suggested_fix or "")
    assert "isn't in this PDB" not in (v.reason or "")


@pytest.mark.parametrize("slug", _SINGLE_CHAIN_TOOLS)
def test_single_chain_tools_still_accept_one_chain(slug):
    """The gate keys on chain COUNT, not on the flags alone. Every gated tool's
    normal single-chain case is untouched — that is the whole product."""
    v = preflight_for_tool(
        slug, _two_chain_target(), target_chain="A", hotspots=[40, 60],
    )
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK), (
        slug, v.kind, v.reason
    )


@pytest.mark.parametrize("slug", _SINGLE_CHAIN_TOOLS)
def test_repeated_chain_id_is_one_chain(slug):
    """"A A" names one chain, not two. De-duping happens before the count so a
    doubled token is not mistaken for a multi-chain target."""
    v = preflight_for_tool(
        slug, _two_chain_target(), target_chain="A A", hotspots=[40, 60],
    )
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK), (
        slug, v.kind, v.reason
    )


@pytest.mark.parametrize("slug", _MULTI_CHAIN_TOOLS)
def test_container_ready_tools_are_not_gated(slug):
    """proteina genuinely handles multi-chain and must stay open. A gate that
    blocks it would break the feature that just shipped."""
    v = preflight_for_tool(
        slug, _two_chain_target(), target_chain="A B", hotspots=[40, 60],
    )
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK), (
        slug, v.kind, v.reason
    )


def test_proteina_still_takes_a_three_chain_target():
    """The shipped capability is a 3-chain target, not merely 2 — pin the case
    the live A100 run actually proved."""
    data = _multi_chain_pdb({
        "A": list(range(1, 41)),
        "B": list(range(1, 41)),
        "C": list(range(1, 41)),
    })
    v = preflight_for_tool(
        "proteina", data, target_chain="A B C", hotspots=[],
    )
    assert v.kind in (VerdictKind.READY, VerdictKind.READY_WITH_FALLBACK), (
        v.kind, v.reason
    )


def test_the_gate_is_driven_by_the_rules_not_by_a_slug_list():
    """Pin the wiring, not the outcome: the set of tools that refuse a
    two-chain target is exactly the set whose rules fail the conjunction. A
    future tool inherits the gate without anyone editing pdb_preflight."""
    from shared.pdb_preflight_rules import TOOL_RULES

    data = _two_chain_target()
    refused = {
        slug for slug in BINDER_DESIGN_TOOLS
        if not preflight_for_tool(
            slug, data, target_chain="A B", hotspots=[40, 60],
        ).ok
    }
    not_allowed_by_rules = {
        slug for slug, r in TOOL_RULES.items()
        if not (r.multi_chain_supported and r.multi_chain_container_ready)
    }
    assert refused == not_allowed_by_rules
    assert refused == set(_SINGLE_CHAIN_TOOLS)


def test_container_readiness_never_outruns_model_support():
    """The invariant between the two flags: you cannot ship an image that does
    something the model cannot do. Guards against a future edit that flips
    container_ready on a tool whose model genuinely can't take multi-chain,
    which would open the gate on the strength of the wrong fact."""
    from shared.pdb_preflight_rules import TOOL_RULES

    for slug, r in TOOL_RULES.items():
        if r.multi_chain_container_ready:
            assert r.multi_chain_supported, (
                f"{slug}: container_ready=True but supported=False"
            )


def test_the_gate_fails_closed_when_the_invariant_is_violated(monkeypatch):
    """WHY the gate reads BOTH flags rather than container_ready alone.

    Given the invariant above, `supported and container_ready` is equivalent to
    `container_ready` for every rule set that satisfies it — which is why the
    `supported` operand can look redundant and be deleted with a green suite.
    Its job is the case where the invariant does NOT hold: a rules edit that
    turns an image on for a model that cannot do the work. Then the conjunction
    still refuses, and dropping the operand would let it through.

    This is the test that makes the read load-bearing. Delete
    `rules.multi_chain_supported and` from the gate and this goes red."""
    from dataclasses import replace

    from shared.pdb_preflight_rules import TOOL_RULES

    # proteina is the one tool with container_ready=True. Violate the invariant
    # on it: the image claims multi-chain, the model says it cannot.
    broken = replace(TOOL_RULES["proteina"], multi_chain_supported=False)
    monkeypatch.setitem(TOOL_RULES, "proteina", broken)

    v = preflight_for_tool(
        "proteina", _two_chain_target(), target_chain="A B", hotspots=[],
    )
    assert not v.ok, (
        "gate allowed a multi-chain run for a tool whose model does not "
        "support it — the `multi_chain_supported` operand is not being read"
    )
    # And it must refuse for the MODEL reason, not the image one.
    assert "image" not in (v.reason or "").lower(), v.reason


def test_only_proteina_is_container_ready_today():
    """A tripwire on the stopgap: today exactly one tool is container-ready."""
    from shared.pdb_preflight_rules import TOOL_RULES

    ready = {s for s, r in TOOL_RULES.items() if r.multi_chain_container_ready}
    assert ready == {"proteina"}, (
        f"container-ready set changed to {sorted(ready)}. If llm-proteinDesigner's "
        f"multi-chain normalizer has been ported and these images rebuilt, that "
        f"is the intended outcome: update this test and the stopgap note in "
        f"shared/pdb_preflight.py. If not, a flag was flipped without the "
        f"container being able to honour it — revert it, because the gate is "
        f"the only thing stopping a funded run that dies in the container."
    )


@pytest.mark.parametrize("slug", _SINGLE_CHAIN_TOOLS)
def test_the_refusal_distinguishes_a_model_limit_from_an_image_limit(slug):
    """An IMAGE limit is temporary, a MODEL limit is permanent, and the user
    acts differently on each. rfantibody must never be described as blocked by
    its "GPU image" — that reads as "coming soon" for a capability that is
    never coming, and someone waits for it.

    Asserted as presence AND absence per branch, because a single shared
    phrase ("one target chain") appears in both templates and a containment
    check alone passes even when both branches emit the same text."""
    from shared.pdb_preflight_rules import TOOL_RULES

    v = preflight_for_tool(
        slug, _two_chain_target(), target_chain="A B", hotspots=[40, 60],
    )
    reason = (v.reason or "").lower()
    assert not v.ok
    if TOOL_RULES[slug].multi_chain_supported:
        # Image-limited: say so, and say "yet".
        assert "image" in reason, (slug, v.reason)
        assert "yet" in reason, (slug, v.reason)
    else:
        # Model-limited: must NOT borrow the temporary-sounding language.
        assert "image" not in reason, (
            f"{slug} is model-limited but its refusal blames the GPU image, "
            f"which promises a capability that will never arrive: {v.reason!r}"
        )
        assert "yet" not in reason, (
            f"{slug} is model-limited but its refusal says 'yet': {v.reason!r}"
        )


def test_proteina_is_only_recommended_when_it_is_actually_available(monkeypatch):
    """proteina is flag-gated (FLAG_GATED_CAMPAIGN_TOOLS). Recommending a tool
    the user cannot see is worse than saying nothing, so the suggestion is
    conditional on the flag — which is fail-closed."""
    data = _two_chain_target()

    monkeypatch.setenv("FLAG_TOOL_PROTEINA", "on")
    on = preflight_for_tool(
        "rfdiffusion", data, target_chain="A B", hotspots=[40, 60],
    )
    assert "Proteina" in (on.suggested_fix or "")

    monkeypatch.setenv("FLAG_TOOL_PROTEINA", "off")
    off = preflight_for_tool(
        "rfdiffusion", data, target_chain="A B", hotspots=[40, 60],
    )
    assert "Proteina" not in (off.suggested_fix or "")
    # The actionable half must survive either way.
    assert "Enter one chain" in (off.suggested_fix or "")

    monkeypatch.delenv("FLAG_TOOL_PROTEINA", raising=False)
    missing = preflight_for_tool(
        "rfdiffusion", data, target_chain="A B", hotspots=[40, 60],
    )
    assert "Proteina" not in (missing.suggested_fix or "")


@pytest.mark.parametrize("slug", _SINGLE_CHAIN_TOOLS)
def test_multi_chain_is_refused_at_submit(slug):
    """End-to-end through the gate that actually protects the wallet.

    blueprints/tools.py::tool_submit blocks on `not verdict.ok` and releases
    the wallet hold. Assert on the same property the route reads, against the
    same entry point it calls, so this cannot pass on a verdict shape the
    route would ignore."""
    from shared.pdb_preflight import PREFLIGHT_TOOLS

    assert slug in PREFLIGHT_TOOLS   # the route only gates members
    v = preflight_for_tool(
        slug, _two_chain_target(), target_chain="A B", hotspots=[40, 60],
        binder_max_aa=120, num_designs=4,
    )
    assert not v.ok
    assert v.reason  # the route surfaces reason (+ suggested_fix) to the user
