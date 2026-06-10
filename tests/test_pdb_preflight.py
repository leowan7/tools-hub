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
    assert BINDER_DESIGN_TOOLS == frozenset({
        "rfantibody", "rfdiffusion", "bindcraft", "boltzgen", "pxdesign",
    })


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
