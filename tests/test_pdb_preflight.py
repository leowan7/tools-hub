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
# llm-proteinDesigner, whose backend/pdb_utils/pipeline_normalize.py used to
# exact-match the chain and raise on "A B". So the fix did not grant a
# capability; it removed a free refusal and would have replaced it with a
# funded run that dies in the container. Hence two flags, and a gate that
# needs both.
#
# THE STOPGAP'S EXIT CONDITION HAS NOW BEEN MET FOR THREE TOOLS.
# leowan7/llm-proteinDesigner#11 ports the multi-chain normalizer
# (parse_target_chains) into that repo, and the images were rebuilt and run on
# a live A100 against a real two-chain target (4ZQK, PD-1/PD-L1) on 2026-08-05:
#
#   rfdiffusion  {A:115, B:106, C:56}  153 s
#   boltzgen     {A:115, B:106, C:55}  430 s
#   pxdesign     {A:115, B:106, C:80}  445 s
#
# Asserted on the RETURNED STRUCTURE, not the status field — a job that
# silently designed against one protomer would still report COMPLETED with
# plausible scores. Both protomers intact, exactly one binder chain, no
# target/binder swap. So those three move to the ready side.
#
# Two tools stay gated, for DIFFERENT reasons, and the distinction is the whole
# point of having two flags:
#   bindcraft   — image limit. Ships from a separate prebuilt image
#                 (kendrew-bindcraft:v7) that never runs llm-pd's normalizer,
#                 and it is the one binder tool with no smoke tier, so the only
#                 way to clear it is a full paid pilot. Temporary: "not yet".
#   rfantibody  — model limit. Builds a VHH against one chain by construction.
#                 Permanent: never.
# ---------------------------------------------------------------------------

# Tools whose container is NOT multi-chain ready today. Deliberately spelled
# out rather than derived from TOOL_RULES: if someone flips a flag, these
# tests must FAIL and make them justify it, not silently follow along.
_SINGLE_CHAIN_TOOLS = ["rfantibody", "bindcraft"]
_MULTI_CHAIN_TOOLS = ["proteina", "rfdiffusion", "boltzgen", "pxdesign"]


def _two_chain_target() -> bytes:
    """Two full, healthy 60-residue chains. Big enough to clear every tool's
    min_target_aa (30) and small enough to clear every hard cap, so the only
    thing a verdict can be reacting to is the chain COUNT.

    WHY IT IS 60 AND NOT 120 EACH. These chains were 120 residues each when the
    multi-chain gate landed. They were then halved because proteina's cap had
    dropped to 140 and 240 was over it, which turned
    `test_container_ready_tools_are_not_gated[proteina]` and
    `test_the_gate_is_driven_by_the_rules_not_by_a_slug_list` red — proteina
    refused for SIZE inside tests whose entire subject is CHAIN COUNT.

    THAT CONSTRAINT IS GONE. proteina's cap is 500 now, measured, so 240 would
    fit comfortably again; 120 total is simply left alone because resizing the
    fixture would churn every multi-chain test for no gain. What still matters
    is only the invariant in the first paragraph: the total must stay under the
    SMALLEST hard cap across all six tools (500 today) and under the smallest
    soft warn (300), so a verdict here can never be reacting to size. At 120
    there is a wide margin on both. If it ever creeps up to where that stops
    being true, these tests will report a multi-chain regression that is not
    there.
    """
    return _multi_chain_pdb({
        "A": list(range(1, 61)),
        "B": list(range(1, 61)),
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


def test_container_ready_set_is_exactly_what_has_been_run_on_a_gpu():
    """A tripwire on the gate: container_ready must track GPU EVIDENCE.

    This deliberately hardcodes the set rather than deriving it, so flipping a
    flag cannot silently follow along — it has to come here and be justified.

    The original form of this test asserted ``ready == {"proteina"}`` and its
    failure message named the exit condition: "If llm-proteinDesigner's
    multi-chain normalizer has been ported and these images rebuilt, that is
    the intended outcome: update this test and the stopgap note." That is what
    happened — leowan7/llm-proteinDesigner#11 plus live two-chain 4ZQK runs on
    2026-08-05 for the three added here. The other half of that message still
    stands: if a slug appears below WITHOUT a GPU run behind it, revert it,
    because this gate is the only thing between a user and a funded run that
    dies in the container.
    """
    from shared.pdb_preflight_rules import TOOL_RULES

    ready = {s for s, r in TOOL_RULES.items() if r.multi_chain_container_ready}
    assert ready == {"proteina", "rfdiffusion", "boltzgen", "pxdesign"}, (
        f"container-ready set changed to {sorted(ready)}. Every slug in this "
        f"set must have a real multi-chain GPU run behind it, asserted on the "
        f"returned structure (both protomers present at full length, exactly "
        f"one binder chain). bindcraft is the live example of what does NOT "
        f"qualify: its wrapper looks correct and it has never been run."
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
        "bindcraft", data, target_chain="A B", hotspots=[40, 60],
    )
    assert "Proteina" in (on.suggested_fix or "")

    monkeypatch.setenv("FLAG_TOOL_PROTEINA", "off")
    off = preflight_for_tool(
        "bindcraft", data, target_chain="A B", hotspots=[40, 60],
    )
    assert "Proteina" not in (off.suggested_fix or "")
    # The actionable half must survive either way.
    assert "Enter one chain" in (off.suggested_fix or "")

    monkeypatch.delenv("FLAG_TOOL_PROTEINA", raising=False)
    missing = preflight_for_tool(
        "bindcraft", data, target_chain="A B", hotspots=[40, 60],
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


# ---------------------------------------------------------------------------
# proteina size envelope — THE COST GATE.
#
# Until bring-your-own targets landed, proteina was reachable only with curated
# ~115 aa benchmark targets, so it sat outside BINDER_DESIGN_TOOLS and got no
# size check at all. With uploads, an oversized target runs to
# _MAX_SESSION_S = 7200, is killed, and bills the full per-shard ceiling
# (~$12.58) for zero designs — across a 4-shard first wave
# (_LAUNCH_CONCURRENCY_OVERRIDE["proteina"] = 4), all of it inside the
# ~$15/shard wallet hold. This envelope is the only thing that refuses that,
# and it shipped with ZERO tests behind a placeholder cap of 600.
#
# The numbers pinned here come from three paid A100-80GB canary shards, all
# completed, all with JAX preallocation disabled so they are comparable to each
# other: 130 aa -> 8,943 MB / 576 s, 260 aa -> 15,541 MB / 645 s, 415 aa ->
# 25,457 MB / 874 s, on an 81,920 MB card. They yield a real slope, and the cap
# is set from it. The earlier 67,546 / 67,570 MB pair that used to be quoted
# here measured a JAX allocator constant rather than this workload and is not
# used for anything. Full provenance in
# shared/pdb_preflight_rules.py::_PROTEINA.
# ---------------------------------------------------------------------------

# The SMALLEST of the three measured sizes, and the one the Fc fixture below
# reproduces exactly.
_PROTEINA_MEASURED_AA = 130
# The LARGEST measured size — where measurement stops and the fit starts, and
# therefore where the soft warn sits.
_PROTEINA_MEASURED_MAX_AA = 415

# Real 3S7G (IgG1 Fc) author numbering — the motivating target.
_FC_CHAINS = {"A": (236, 443), "B": (236, 442), "C": (237, 444), "D": (238, 444)}


def _fc_pdb(*chains, last=None) -> bytes:
    """A synthetic stand-in for 3S7G carrying its real per-chain residue spans.

    ``last`` overrides a chain's final resnum, so a sub-domain selection (the
    canaried window, say) can be built without inventing a numbering scheme.
    """
    spec = {}
    for c in chains:
        lo, hi = _FC_CHAINS[c]
        if last and c in last:
            hi = last[c]
        spec[c] = list(range(lo, hi + 1))
    return _multi_chain_pdb(spec)


def _proteina_size(target_aa: int, **kw):
    """Verdict for a single-chain proteina target of exactly ``target_aa``."""
    data = _chain_pdb("A", list(range(1, target_aa + 1)))
    return preflight_for_tool(
        "proteina", data, target_chain="A", hotspots=[], **kw
    )


def test_proteina_size_envelope_constants_are_pinned():
    """A money constant must not drift without someone re-reading its evidence.

    It already did twice: the cap shipped at 600 as a placeholder that was
    never re-set from measurement, and then at 140, which was policy anchored
    to a JAX allocator constant rather than to this workload. 500 is anchored
    to the three-point scaling curve in _PROTEINA's comment — 1.2x beyond the
    largest size actually measured (415), landing at ~39% of an A100-80GB by
    that curve.

    It is still a POLICY number and it may be raised again, but only the same
    way it was set: by one completed shard above 415 aa. Do not re-derive it
    from an argument, from a longer extrapolation of these same three points,
    or from any reading taken while JAX preallocation was on.
    """
    from shared.pdb_preflight_rules import TOOL_RULES
    rules = TOOL_RULES["proteina"]
    env = rules.size
    assert env.hard_cap_target_aa == 500
    assert env.soft_warn_target_aa == 415
    assert env.hard_cap_combined_aa == 620
    # Structural invariants, independent of the exact numbers.
    assert env.soft_warn_target_aa < env.hard_cap_target_aa
    assert rules.min_target_aa < env.hard_cap_target_aa
    assert rules.gpu == "A100-80GB"
    # The soft warn IS the measurement boundary, not a fraction of the cap —
    # 60% of 500 would be 300 and would warn on sizes that have been RUN.
    assert env.soft_warn_target_aa == _PROTEINA_MEASURED_MAX_AA


# The three completed A100-80GB canary shards the envelope is derived from,
# every one with JAX preallocation disabled: (target_aa, peak device VRAM MB,
# runtime s). protein_binder, seed 1234, 8 designs, binder_length [60, 120].
_PROTEINA_CANARY = ((130, 8_943, 576), (260, 15_541, 645), (415, 25_457, 874))

_A100_80GB_MB = 81_920
# What JAX reserves on its first op when PREALLOCATE is left at its default
# (MEM_FRACTION=0.75). A device reading at or above this floor is an allocator
# artifact, not a measurement of this workload — which is exactly what the two
# retired 67,5xx MB readings were.
_JAX_PREALLOC_MB = 61_440


def _quadratic_through_canary(n: float) -> float:
    """The exact quadratic through the three measured VRAM points, in MB.

    Lagrange rather than hardcoded coefficients, so this also checks the
    ``MB = 3913 + 32.66*n + 0.04639*n^2`` written in _PROTEINA's comment
    instead of trusting it.
    """
    pts = [(float(aa), float(mb)) for aa, mb, _ in _PROTEINA_CANARY]
    total = 0.0
    for i, (xi, yi) in enumerate(pts):
        term = yi
        for j, (xj, _) in enumerate(pts):
            if i != j:
                term *= (n - xj) / (xi - xj)
        total += term
    return total


def _runtime_min(base: float, alpha: float, aa: int) -> float:
    """The estimator's own curve at 8 designs, i.e. design_factor == 1."""
    return base * (aa / 120.0) ** alpha


def test_the_proteina_cap_is_traceable_to_three_post_prealloc_shards():
    """PROVENANCE, not value. ``..._constants_are_pinned`` guards WHAT the
    numbers are; this guards WHERE THEY CAME FROM, which is the part that has
    gone wrong every previous time.

    Three independent ways THIS ENVELOPE could be re-derived wrongly, all of
    which have precedent on this tool, and all of which this test refuses.
    Read the scope of each — they are not all about the same number:

    1. FROM A PREALLOCATION-ERA READING. The two shards before these read
       67,546 and 67,570 MB and agreed to 24 MB across a doubled chain count,
       because ~91% of each was the JAX allocator constant. Any such reading
       is necessarily at or above that 61,440 MB floor, and a constant-
       dominated set cannot spread with target size. Both properties are
       asserted, so a reading taken with preallocation back on cannot be
       substituted into this table unnoticed. This one IS about the VRAM
       readings the size cap comes from.

    2. FROM A TWO-POINT FIT. Two points always fit a power law exactly, which
       is precisely why two points prove nothing. Every pair here is refitted
       and shown to MISS the omitted third shard by more than 10%, so the
       curve genuinely required all three. DEMONSTRATED ON THE RUNTIME CURVE,
       in block (4) below — that is the fit whose parameters actually ship
       (runtime_base_min / runtime_alpha) and the only one this table can be
       refitted into. It establishes that these three shards are not
       collinear enough for any two of them to stand in for the third; it is
       not itself an assertion about hard_cap_target_aa, and should not be
       read as one.

    3. BY EXTRAPOLATING FURTHER THAN THE EVIDENCE CARRIES. The cap is held to
       a modest step beyond the largest MEASURED size, not to wherever the fit
       stops predicting an OOM (~992 aa). About the cap again.
    """
    from shared.pdb_preflight_rules import TOOL_RULES
    env = TOOL_RULES["proteina"].size
    sizes = [aa for aa, _, _ in _PROTEINA_CANARY]
    peaks = [mb for _, mb, _ in _PROTEINA_CANARY]

    # (1) Post-fix regime. Every reading is far below the allocator floor, and
    # the readings SPREAD with target size instead of agreeing to noise.
    for aa, mb in zip(sizes, peaks):
        assert mb < _JAX_PREALLOC_MB, (
            f"the {aa} aa reading ({mb} MB) is at or above JAX's "
            f"preallocation floor — that is an allocator constant, not this "
            f"workload"
        )
    assert peaks[-1] >= 2.0 * peaks[0], (
        f"VRAM barely moved across a {sizes[-1] / sizes[0]:.1f}x target range "
        f"({peaks[0]} -> {peaks[-1]} MB); that is the signature of a constant "
        f"dominating the reading, which is what invalidated the last pair"
    )

    # (2) Growth ACCELERATES, so no straight line through the low end may be
    # used to extrapolate — it would under-read, the direction that bills.
    import math
    exp_low = (math.log(peaks[1] / peaks[0])
               / math.log(sizes[1] / sizes[0]))
    exp_high = (math.log(peaks[2] / peaks[1])
                / math.log(sizes[2] / sizes[1]))
    assert exp_high > exp_low, (
        f"VRAM exponent fell from {exp_low:.2f} to {exp_high:.2f}; the comment "
        f"claims accelerating growth and the cap's headroom assumes it"
    )

    # (3) The shipped runtime curve reproduces ALL THREE shards within 10%.
    for aa, _, secs in _PROTEINA_CANARY:
        est = _runtime_min(env.runtime_base_min, env.runtime_alpha, aa)
        residual = abs(est - secs / 60.0) / (secs / 60.0)
        assert residual <= 0.10, (
            f"base={env.runtime_base_min} alpha={env.runtime_alpha} puts the "
            f"{aa} aa shard at {est:.1f} min against a measured "
            f"{secs / 60.0:.1f} ({residual:.0%} out)"
        )

    # (4) And two of the three points could not have produced that RUNTIME
    # curve. Each pair is refitted exactly and misses the shard it omitted.
    # See the scope note in the docstring: this is the two-point argument, run
    # on the fit whose parameters ship, not a second guard on the VRAM cap.
    for omit in range(3):
        (a1, _, s1), (a2, _, s2) = [
            p for i, p in enumerate(_PROTEINA_CANARY) if i != omit
        ]
        alpha = math.log((s2 / 60.0) / (s1 / 60.0)) / math.log(a2 / a1)
        base = (s1 / 60.0) / (a1 / 120.0) ** alpha
        held_aa, _, held_s = _PROTEINA_CANARY[omit]
        pred = _runtime_min(base, alpha, held_aa)
        miss = abs(pred - held_s / 60.0) / (held_s / 60.0)
        assert miss > 0.10, (
            f"a fit through only {a1} and {a2} aa already predicts the "
            f"{held_aa} aa shard to within {miss:.0%} — if that were true the "
            f"third shard was unnecessary, and this test is the wrong guard"
        )

    # (5) The cap is a modest step past MEASUREMENT, not past the fit's OOM.
    measured_max = max(sizes)
    assert env.soft_warn_target_aa == measured_max
    assert env.hard_cap_target_aa > measured_max
    assert env.hard_cap_target_aa <= 1.5 * measured_max, (
        f"cap {env.hard_cap_target_aa} is more than a 1.5x step beyond the "
        f"largest measured size ({measured_max}); extrapolating that far "
        f"needs another shard, not another argument"
    )

    # (6) And the modelled load at the cap leaves room for the model to be
    # badly wrong. Doubling it must still fit on the card.
    at_cap = _quadratic_through_canary(env.hard_cap_target_aa)
    assert 30_000 <= at_cap <= 34_000, (
        f"the fit puts the cap at {at_cap:.0f} MB, not the ~31,841 the "
        f"comment claims"
    )
    assert 2.0 * at_cap < _A100_80GB_MB, (
        f"a 100% model error at the cap ({at_cap:.0f} MB) would exhaust the "
        f"card; the headroom argument for this cap no longer holds"
    )
    # The quadratic really is the one the comment writes down.
    assert abs(_quadratic_through_canary(600) - 40_209) < 50


def test_the_proteina_comment_table_is_the_canonical_measurement_table():
    """The table in ``_PROTEINA``'s comment IS the evidence, and it was the one
    copy of it that nothing pinned.

    Every number below that table is argued from it — the quadratic, the two
    power-law exponents, the percentage of the card at the cap, the runtime
    fit, and ultimately hard_cap_target_aa itself. The shipped CONSTANTS are
    pinned, and the canary's copy of the same three rows is pinned
    (tests/test_proteina_canary.py::
    test_the_quoted_measurements_match_the_canonical_table), but the rows the
    constants are justified BY were free to
    move: change 8,943 to 9,943 in that comment and the whole suite stayed
    green, while the next person to re-derive the cap would re-derive it from
    a reading nobody took. That is the same failure the canary-footer pin
    exists for, on the copy more people read.

    Deliberately tolerant of FORMAT and strict about DATA: a row is
    ``aa | MB | % of card | seconds`` in that order with any spacing, so
    re-wrapping or re-aligning the table costs nothing. The percentage column
    is not taken on faith either — it is recomputed against the card size.
    """
    import re as _re

    from shared import pdb_preflight_rules

    src = Path(pdb_preflight_rules.__file__).read_text(encoding="utf-8")
    block = src.split("_PROTEINA = ToolRules(", 1)[1].split(
        "TOOL_RULES: dict", 1)[0]
    rows = _re.findall(
        r"^\s*#\s*(\d+)\s+([\d,]+)\s*MB\s+([\d.]+)\s*%\s+(\d+)\s*s\s*$",
        block, _re.M,
    )
    parsed = [
        (int(aa), int(mb.replace(",", "")), float(pct), int(secs))
        for aa, mb, pct, secs in rows
    ]
    assert [(aa, mb, secs) for aa, mb, _, secs in parsed] == [
        tuple(row) for row in _PROTEINA_CANARY
    ], (
        f"_PROTEINA's comment tabulates {[(a, m, s) for a, m, _, s in parsed]} "
        f"but the envelope is derived from {list(_PROTEINA_CANARY)}; the "
        f"argument for the cap and the data behind it have separated"
    )
    for aa, mb, pct, _ in parsed:
        expected = round(mb / _A100_80GB_MB * 100, 1)
        assert abs(pct - expected) < 0.05, (
            f"the {aa} aa row calls {mb} MB {pct}% of the card; on "
            f"{_A100_80GB_MB} MB it is {expected}%"
        )


def test_proteina_cap_still_admits_the_size_we_actually_measured():
    """The guard against over-correcting. 130 aa across 2 chains is the
    smallest of the three measured shards (8,943 MB of 81,920, 576 s wall); a
    cap that refused it would be refusing something we know works."""
    data = _fc_pdb("A", "B", last={"A": 300, "B": 300})   # 65 + 65 = 130 aa
    v = preflight_for_tool("proteina", data, target_chain="A B", hotspots=[])
    assert v.size_envelope.residue_count == _PROTEINA_MEASURED_AA
    assert v.kind is not VerdictKind.NEEDS_FIX
    assert not v.size_envelope.over_hard_cap
    assert not v.size_envelope.over_soft_warn


def test_proteina_hard_cap_boundary_is_exact():
    """``over_hard`` is a strict > on the cap, so 500 runs and 501 does not.
    Pins the off-by-one: a gate that fires one residue early or late is a
    different gate than the one the evidence supports."""
    at_cap = _proteina_size(500)
    assert not at_cap.size_envelope.over_hard_cap
    assert at_cap.kind is not VerdictKind.NEEDS_FIX

    over = _proteina_size(501)
    assert over.size_envelope.over_hard_cap
    assert over.kind is VerdictKind.NEEDS_FIX


def test_proteina_oversized_target_is_refused_with_an_actionable_reason():
    """The refusal has to name the size, the cap and the GPU — a bare "too
    big" leaves the user with no way to pick a selection that would run."""
    v = _proteina_size(600)
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.size_envelope.over_hard_cap
    assert "600" in v.reason
    assert "500" in v.reason
    assert "A100-80GB" in v.reason
    assert v.suggested_fix


def test_proteina_soft_warn_starts_above_the_measured_size():
    """Between the largest measured size (415) and the 500 cap a job is
    allowed but flagged: that band is where the fit is talking rather than a
    run, which is a different claim from "known good"."""
    v = _proteina_size(450)
    assert v.kind is not VerdictKind.NEEDS_FIX
    assert v.size_envelope.over_soft_warn
    assert not v.size_envelope.over_hard_cap
    assert "450" in (v.size_envelope.warn_message or "")


def test_proteina_multi_chain_target_is_summed_not_taken_per_chain():
    """An Fc is big BECAUSE it is several chains. If the cap looked at one
    chain at a time, a 4 x 207 aa tetramer would read as 207 and sail through —
    which is the whole exposure this envelope exists to close. Both chains here
    are far under the 500 cap on their own; only the sum crosses it."""
    under = _multi_chain_pdb({
        "A": list(range(1, 251)), "B": list(range(1, 251)),
    })
    v_under = preflight_for_tool(
        "proteina", under, target_chain="A B", hotspots=[],
    )
    assert v_under.size_envelope.residue_count == 500
    assert not v_under.size_envelope.over_hard_cap

    over = _multi_chain_pdb({
        "A": list(range(1, 252)), "B": list(range(1, 252)),
    })
    v_over = preflight_for_tool(
        "proteina", over, target_chain="A B", hotspots=[],
    )
    assert v_over.size_envelope.residue_count == 502
    assert v_over.size_envelope.over_hard_cap
    assert v_over.kind is VerdictKind.NEEDS_FIX


def test_proteina_admits_the_typical_fc_ch2_ch3_selection():
    """THE SUBMISSION THE FEATURE WAS BUILT FOR, AND IT NOW RUNS.

    3S7G chains A+B over their full CH2+CH3 span is 415 aa. Under the old 140
    cap this was refused on purpose, and the test that pinned the refusal said
    the raise "must cite a canary run, not an argument". It cites one: 415 aa
    is the largest of the three completed shards, 25,457 MB of 81,920 (31.1%)
    in 874 s.

    It sits exactly ON the soft warn, and ``over_warn`` is a strict >, so a
    measured size must not carry the amber "not measured" notice either.
    """
    data = _fc_pdb("A", "B")
    v = preflight_for_tool("proteina", data, target_chain="A B", hotspots=[])
    assert v.size_envelope.residue_count == _PROTEINA_MEASURED_MAX_AA
    assert not v.size_envelope.over_hard_cap
    assert not v.size_envelope.over_soft_warn
    assert v.kind is not VerdictKind.NEEDS_FIX


def test_proteina_refuses_the_whole_fc_file():
    """All four 3S7G chains = 830 aa: 2x the largest size measured, and well
    over the 500 cap. The fit does NOT say this one OOMs — it puts 830 aa at
    ~77% of the card — which is the point. The cap refuses it because it is
    far outside measurement, not because a failure is predicted there."""
    data = _fc_pdb("A", "B", "C", "D")
    v = preflight_for_tool("proteina", data, target_chain="A B C D", hotspots=[])
    assert v.size_envelope.residue_count == 830
    assert v.size_envelope.over_hard_cap
    assert v.kind is VerdictKind.NEEDS_FIX


def test_proteina_combined_budget_blocks_an_unmeasured_binder_length():
    """The canaries ran binder_length [60, 120] at every one of the three
    measured target sizes. The form allows up to 300, and a 300-aa binder is a
    complex nobody has measured, so the combined budget refuses it even though
    the TARGET (400 aa, inside the measured span) is legal on its own."""
    v = _proteina_size(400, binder_max_aa=300)
    assert not v.size_envelope.over_hard_cap
    assert v.size_envelope.over_combined_cap
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.size_envelope.combined_aa == 700

    # The binder range that WAS measured still passes at the same target.
    ok = _proteina_size(400, binder_max_aa=120)
    assert not ok.size_envelope.over_combined_cap
    assert ok.kind is not VerdictKind.NEEDS_FIX


def test_proteina_runtime_estimate_is_anchored_to_the_measured_shard():
    """576 s (9.6 min) for 8 designs at 130 aa is the smallest of the three
    measured wall-clocks, so the advisory estimate has to land on it. It has
    been wrong twice before in the copy users plan against: first anchored to
    meta.py's invented "30 to 120 min" band (~83 min for this shard), then to a
    359 s reading at the same size (6.0 min) — the other of two 130 aa readings
    that disagree by ~60%. Both are recorded as completed 8-design shards; what
    separates them is the JAX allocator regime (359 s with preallocation on,
    576 s with it off), which shared/pdb_preflight_rules.py::_PROTEINA
    documents as non-comparable. 576 s is the reading taken under the allocator
    settings production runs today, which is why the estimate has to reproduce
    it and not the other one.

    THE BAND IS +/-5%, AND IT IS ANCHORED TO THIS POINT'S RESIDUAL, not to the
    worst residual anywhere on the fit. The shipped curve puts 130 aa at
    9.25 min against the measured 9.6 — 3.7% low — so +/-5% is the tightest
    round band that admits the fit HERE. The 260 aa point runs out to ~9%
    (test_the_proteina_cap_is_traceable_to_three_post_prealloc_shards, block 3,
    is where that looser bound belongs), and importing it into this assertion
    is not a correction, it is lost coverage. EXECUTED, not reasoned: at
    +/-10% the lower bound falls to 8.64, and a runtime_base_min of 8.7 —
    which models this shard at 8.94 min — then leaves the ENTIRE suite green,
    with no other test anywhere refusing it.
    """
    data = _fc_pdb("A", "B", last={"A": 300, "B": 300})   # the measured 130 aa
    v = preflight_for_tool(
        "proteina", data, target_chain="A B", hotspots=[], num_designs=8,
    )
    est = v.size_envelope.runtime_estimate_min
    assert est is not None
    # 576 s = 9.6 min. Both bounds are clear of the estimator's max(5.0, ...)
    # floor, so this cannot be satisfied by the floor instead of the anchor.
    assert 9.12 <= est <= 10.08, f"estimate {est} min is not the measured 9.6"


def test_proteina_runtime_curve_bends_with_target_size():
    """Pins ``runtime_alpha``. Doubling the target multiplies the estimate by
    2^alpha, so this is the only place the exponent is visible — every
    single-size assertion is blind to it.

    alpha was 1.3 and labelled ASSUMED (borrowed from pxdesign, because one
    target size cannot yield an exponent). Three sizes gave a measured 0.34:
    proteina's runtime is far flatter in target size than the borrowed curve
    claimed, and the measurement is what says so.
    """
    from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
    rules = TOOL_RULES["proteina"]
    ratio = (
        runtime_estimate_min(rules, 260, 8)
        / runtime_estimate_min(rules, 130, 8)
    )
    # 2 ** 0.34 = 1.266. The retired alpha=1.3 gives 2.46 and a flat 1.0 gives
    # 2.0 — both far outside this band, which is the point.
    assert 1.24 <= ratio <= 1.30, f"alpha is not 0.34 (size ratio {ratio:.3f})"
    # And it really is a bend, not a constant: 130 -> 415 must cost more.
    assert (
        runtime_estimate_min(rules, 415, 8)
        > runtime_estimate_min(rules, 130, 8)
    )


def test_proteina_runtime_scales_per_SHARD_not_per_hundred_designs():
    """Pins ``runtime_baseline_designs=8``. proteina's shard IS 8 designs
    (_SHARD_DESIGNS); the dataclass default is 100. Getting that wrong scales
    every estimate by 12.5x, and at ordinary design counts the estimator's
    5-minute floor hides it — so this asserts at a count far above the floor,
    where the two cannot be confused."""
    from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
    rules = TOOL_RULES["proteina"]
    assert rules.size.runtime_baseline_designs == 8
    est = runtime_estimate_min(rules, 130, 800)      # 100 shards
    # 100 x the measured 9.6 min shard. With baseline 100 it would be ~74 min.
    assert 880.0 <= est <= 1010.0, f"800 designs estimated at {est} min"


def test_proteina_over_cap_target_reports_only_the_cap_breach():
    """The status flags are a precedence ladder, not independent booleans.

    ``over_soft_warn`` and ``over_combined_cap`` are suppressed once the hard
    cap fires, because the panel renders whichever is set and a user who is
    told BOTH "this is above the comfort zone" and "this is over the limit"
    gets a mixed verdict on a job that simply cannot run. Nothing else in the
    suite exercised the suppression, so dropping either clause was invisible.
    """
    v = _proteina_size(600, binder_max_aa=300)       # over hard AND combined
    assert v.kind is VerdictKind.NEEDS_FIX
    assert v.size_envelope.over_hard_cap
    assert not v.size_envelope.over_soft_warn, (
        "a target over the hard cap must not also report a soft warning")
    assert not v.size_envelope.over_combined_cap, (
        "the hard cap outranks the combined budget in the message ladder")
    # And the message the user sees is the cap one, not the combined one.
    assert "combined" not in (v.reason or "").lower()


def test_proteina_combined_budget_boundary_is_exact():
    """Pins ``hard_cap_combined_aa`` BEHAVIOURALLY, so the literal pin in
    ``test_proteina_size_envelope_constants_are_pinned`` is not the only thing
    standing between this number and a silent edit. 500 + 120 = 620 is exactly
    the budget and runs; one residue more does not. The 120 is the top of the
    binder range every canary shard actually used."""
    at_budget = _proteina_size(500, binder_max_aa=120)
    assert at_budget.size_envelope.combined_aa == 620
    assert not at_budget.size_envelope.over_combined_cap
    assert at_budget.kind is not VerdictKind.NEEDS_FIX

    over = _proteina_size(500, binder_max_aa=121)
    assert over.size_envelope.combined_aa == 621
    assert over.size_envelope.over_combined_cap
    assert over.kind is VerdictKind.NEEDS_FIX


def test_proteina_soft_warn_boundary_is_exact():
    """Pins ``soft_warn_target_aa`` behaviourally for the same reason: 415 is
    the largest size that has been RUN, so it must NOT warn, and 416 — the
    first residue count that only the fit has an opinion about — must."""
    assert not _proteina_size(415).size_envelope.over_soft_warn
    assert _proteina_size(416).size_envelope.over_soft_warn


# ---------------------------------------------------------------------------
# Sizing the SELECTION rather than the upload.
#
# prepare_custom_target stages the whole file and the contig selects what
# reaches the model, so counting the upload refused runs that fit — including
# the exact 130 aa configuration the paid canary validated, when it was reached
# by uploading whole 3S7G and narrowing with a contig. Hand-trimming the PDB
# was the only way through, which is work the contig exists to remove.
# ---------------------------------------------------------------------------

def test_proteina_sizes_the_contig_selection_not_the_uploaded_file():
    """Whole 3S7G (830 aa) narrowed to the canaried 130 aa window runs."""
    data = _fc_pdb("A", "B", "C", "D")               # 830 aa uploaded
    v = preflight_for_tool(
        "proteina", data, target_chain="A B", hotspots=[],
        target_segments=[("A", 236, 300), ("B", 236, 300)],
    )
    assert v.size_envelope.residue_count == 130
    assert v.size_envelope.size_basis == "selection"
    assert not v.size_envelope.over_hard_cap
    assert v.kind is not VerdictKind.NEEDS_FIX


def test_proteina_selection_of_one_domain_from_a_big_upload_runs():
    """A 61-residue window out of the same 830 aa file is a 61-residue run."""
    data = _fc_pdb("A", "B", "C", "D")
    v = preflight_for_tool(
        "proteina", data, target_chain="A", hotspots=[],
        target_segments=[("A", 300, 360)],
    )
    assert v.size_envelope.residue_count == 61
    assert v.kind is not VerdictKind.NEEDS_FIX


def test_proteina_oversized_selection_is_still_refused():
    """Sizing the selection must not become a way to smuggle a big run
    through: the selection itself is what the cap now applies to. Three of
    3S7G's chains is 623 aa, over the 500 cap."""
    data = _fc_pdb("A", "B", "C", "D")
    v = preflight_for_tool(
        "proteina", data, target_chain="A B C", hotspots=[],
        target_segments=[
            ("A", 236, 443), ("B", 236, 442), ("C", 237, 444),
        ],                                                     # 623 aa
    )
    assert v.size_envelope.residue_count == 623
    assert v.size_envelope.over_hard_cap
    assert v.kind is VerdictKind.NEEDS_FIX
    # The message names the region the user typed, not the file.
    assert "A236-443,B236-442,C237-444" in v.reason


def test_proteina_no_contig_still_counts_the_whole_named_chains():
    """No selection declared means the whole chain, which is what the
    container derives — and it over-counts rather than under-counts, so the
    absent case can never be the one that lets an oversized run through."""
    data = _fc_pdb("A", "B", "C", "D")
    for segments in (None, []):
        v = preflight_for_tool(
            "proteina", data, target_chain="A B C", hotspots=[],
            target_segments=segments,
        )
        assert v.size_envelope.residue_count == 623
        assert v.size_envelope.size_basis == "chains"
        assert v.kind is VerdictKind.NEEDS_FIX


def test_proteina_refusal_copy_does_not_predict_an_oom_it_cannot_predict():
    """S5. proteina's cap sits ABOVE everything that has been run (largest:
    415 aa) and its own scaling curve puts the 500 cap at ~39% of the card, so
    the copy may not assert the job "would likely run out of memory" — nothing
    predicts a failure there. Tools whose caps come from published work keep
    the stronger wording; this asserts the two really are different.

    The branch keys on cap_basis, and proteina's is now "measured" rather than
    "untested" — so this also pins that the guard is "only literature-backed
    caps may predict an OOM" rather than an equality with one basis string.
    """
    from shared.pdb_preflight_rules import TOOL_RULES
    assert TOOL_RULES["proteina"].size.cap_basis == "measured"

    proteina = _proteina_size(600)
    assert "out of memory" not in (proteina.reason or "").lower()
    assert "precaution" in (proteina.reason or "").lower()

    # rfdiffusion's cap is literature-backed, so it keeps the OOM prediction.
    big = _chain_pdb("A", list(range(1, 551)))
    rfd = preflight_for_tool(
        "rfdiffusion", big, target_chain="A", hotspots=[100],
    )
    assert "out of memory" in (rfd.reason or "").lower()


def test_proteina_size_refusal_does_not_offer_an_absent_alphafold_model():
    """The fix used to say "Try the AlphaFold model" next to alphafold=None,
    pointing at a control the panel never rendered. Synthetic PDBs carry no
    DBREF, so there is no suggestion to offer here."""
    v = _proteina_size(600)
    assert v.alphafold is None
    assert "alphafold" not in (v.suggested_fix or "").lower()
    assert "narrow" in (v.suggested_fix or "").lower()


def test_size_refusal_pluralises_and_names_what_it_counted():
    """"Target chain has 623 residues" would be wrong twice for a 3-chain
    selection: one chain implied, and the file counted rather than the run."""
    v = preflight_for_tool(
        "proteina", _fc_pdb("A", "B", "C", "D"), target_chain="A B C",
        hotspots=[], target_segments=[
            ("A", 236, 443), ("B", 236, 442), ("C", 237, 444),
        ],
    )
    assert "The region you selected" in v.reason
    assert "623 residues" in v.reason
    one = _proteina_size(1)          # min-residue floor fires, not the cap
    assert one.kind is VerdictKind.NEEDS_FIX


def test_bindcraft_runtime_curve_reproduces_its_one_measured_run():
    """The bindcraft runtime anchor is a single real job, so pin it to that job.

    Job 1c4d5803: 1170 GPU-s for 2 designs against 4ZQK chain A (~115 aa),
    2026-05-28. The figures and their wallet-ledger reconciliation are in the
    worked-example header of tools/bindcraft/meta.py, the primary record is
    the row in docs/VALIDATION-LOG.md, and the same run is cited by
    tools/bindcraft/__init__.py::validate.

    Three things are pinned. The FIRST is the anchor; the other two are the
    ways it rotted or could rot without anything failing.

    ``runtime_alpha`` is NOT pinned to 1.5 and cannot be, because the run is a
    single target size only 4.2% off this curve's 120 aa anchor: (115/120)
    **alpha barely moves over the plausible range, so the residual below is
    nearly blind to the exponent. It is not entirely blind -- solving the
    tolerance gives alpha in about [-0.55, 1.80], so a large drift is caught
    (verified by mutation: alpha=3.0 estimates 17.60 min, 9.7% out, FAILS)
    while 1.5 and 1.0 are indistinguishable here. Treat size scaling as
    unmeasured until a second target size is run. What pinning an exponent
    actually looks like is
    ``test_proteina_runtime_curve_bends_with_target_size`` in this file, and
    it needs two target sizes to do it.

    1. The curve reproduces that run within 5%. The shipped constants put it
       at 18.76 min against 19.5, a 3.8% residual, so +/-5% is the tightest
       round band that admits the fit HERE. That is the rule
       ``test_proteina_runtime_estimate_is_anchored_to_the_measured_shard``
       follows for its single anchor, as against the +/-10% that
       ``test_the_proteina_cap_is_traceable_to_three_post_prealloc_shards``
       uses, where one band must cover the worst residual across three points.
       The old base=300 / baseline=10 constants -- a published ~30
       min/TRAJECTORY rate applied per DESIGN -- put this run at 56 min, 2.9x
       over, so the looser band would have caught them too; what +/-5% adds is
       that it also refuses the quieter drift: executed, base 39.5 and 43.6
       pass while 39.0 and 44.0 fail, where +/-10% passed everything from 37.5
       to 45.0. Both bounds are far clear of the estimator's max(5.0, est)
       floor, so this cannot be satisfied by the floor instead of the anchor.

    2. The estimate at the default design count stays inside the band this
       tool already advertises. ``PRESET_RUNTIME["pilot"]`` is rendered to
       users by shared/tools_catalog.py and blueprints/tools.py, so a curve
       outside it contradicts the page the user read on the way to the form
       -- which is exactly what the old constants did, quoting 120 min
       (300 * (120/120)**1.5 * 4/10) under a catalog promising 30 to 45.
       The band is PARSED, not restated -- the same rule
       tests/test_proteina_shard_size.py::
       test_about_runtime_row_names_the_width_it_was_measured_at follows --
       so moving either surface fails here instead of drifting apart quietly.

    3. ``runtime_baseline_designs`` still equals the form default. It is a
       DIVISOR (shared/pdb_preflight_rules.py::runtime_estimate_min), so it
       only reads as "the estimate at the default design count" while it
       tracks whatever ``validate`` falls back to. It stopped tracking once
       already, silently, when that default moved to 4: nothing failed, the
       panel just quoted a runtime for a design count no form submits. Read
       the fallback out of the validator rather than restating it here, so
       it cannot drift the same way twice.
    """
    import inspect as _inspect
    import re as _re
    from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
    import tools.bindcraft as _bindcraft
    from tools.bindcraft import meta as _meta

    rules = TOOL_RULES["bindcraft"]

    # (1) Reproduce job 1c4d5803. Anchored on the BILLED 1170 GPU-seconds:
    #     that is the figure the wallet ledger reconciles against the rate
    #     card, and tools/bindcraft/example/result.json independently stores
    #     the same run as runtime_minutes 19.5. The job's own timestamps
    #     (18:31:18 to 18:50:51, docs/VALIDATION-LOG.md) span 1173 s, 0.3%
    #     away, so the choice between the two records cannot move this
    #     assertion. A third figure, 19m57s, used to sit in the meta.py
    #     header; it matched neither record and this change corrects it.
    est = runtime_estimate_min(rules, target_aa=115, num_designs=2)
    measured_min = 1170.0 / 60.0
    residual = abs(est - measured_min) / measured_min
    assert residual <= 0.05, (
        f"base={rules.size.runtime_base_min} alpha={rules.size.runtime_alpha} "
        f"baseline={rules.size.runtime_baseline_designs} puts job 1c4d5803 at "
        f"{est:.1f} min against a measured {measured_min:.1f} "
        f"({residual:.0%} out)"
    )

    # (3) The baseline is the validator's own default, read from its source.
    #     Checked before (2) because (2)'s premise is that this IS the
    #     default design count.
    src = _inspect.getsource(_bindcraft.validate)
    m = _re.search(r'form\.get\("num_designs"\)\s*or\s*"(\d+)"', src)
    assert m, (
        "could not find the num_designs fallback in "
        "tools/bindcraft/__init__.py::validate -- if the parse changed, fix "
        "this test rather than deleting it; the drift it guards is silent"
    )
    default_designs = int(m.group(1))
    assert rules.size.runtime_baseline_designs == default_designs, (
        f"runtime_baseline_designs={rules.size.runtime_baseline_designs} but "
        f"validate() defaults num_designs to {default_designs}; the preflight "
        f"panel is quoting a runtime for a design count the form never submits"
    )

    # (2) And the default-count estimate sits inside the advertised band.
    band = str(_meta.PRESET_RUNTIME["pilot"]["typical_minutes"])
    bounds = [float(x) for x in _re.findall(r"\d+(?:\.\d+)?", band)]
    assert len(bounds) == 2, (
        f"PRESET_RUNTIME['pilot']['typical_minutes'] is {band!r}, which is "
        f"not the two-number range this check assumes"
    )
    lo, hi = bounds
    at_default = runtime_estimate_min(
        rules, target_aa=120, num_designs=default_designs
    )
    assert lo <= at_default <= hi, (
        f"the preflight panel estimates {at_default:.1f} min for "
        f"{default_designs} designs while the catalog and About panel "
        f"advertise '{band} min' for that same default"
    )


def test_pxdesign_runtime_curve_anchors_on_one_run_and_discloses_the_rest():
    """pxdesign's runtime anchor is three real pilot runs that do not fit.

    The three, all pilot tier, all passing the user's ``num_designs``
    straight through ``tools.pxdesign.build_payload`` to the container:

        job 79228f03  2026-05-26   462 GPU-s   5 designs   1HEW chain A
        job 816fc4a9  2026-05-27   504 GPU-s   2 designs   4ZQK chain A
        worked example             1380 GPU-s  25 designs  ~420 aa, 2 chains

    The first two are rows in docs/VALIDATION-LOG.md. The third is
    tools/pxdesign/example/result.json, which stores ``runtime_minutes``
    23.0 and ``total_designs`` 25 as data and is reconciled against the
    wallet ledger in the EXAMPLE header of tools/pxdesign/meta.py.

    THE CANDIDATE COUNTS ARE NOT THE DESIGN COUNTS on the first two and
    must not be used as such. Both surfaced exactly 2 candidates, all
    below threshold. The mechanism that produces that shape is the pilot
    fallback emitting top-N when nothing passes the filter, named in the
    comment above the ``job_tier`` key in
    tools/boltzgen/__init__.py::build_payload -- but that is BOLTZGEN's
    comment, gating BOLTZGEN's tier on its own ipTM/pLDDT/RMSD thresholds.
    pxdesign's build_payload sets no ``job_tier`` and its container is not
    in this repo, so "the same fallback ran here" is an INFERENCE from a
    sibling tool, not something this repo records. It is load-bearing: the
    rates below divide by the ``num_designs`` REQUESTED, which assumes all
    of them were generated and scored. Dividing by candidates instead
    would make the two runs agree for the wrong reason.

    WHY THEY DO NOT FIT. Per-design rate falls 4.20 -> 1.54 -> 0.92 min as
    n goes 2 -> 5 -> 25: a fixed overhead, for which
    ``shared/pdb_preflight_rules.py::runtime_estimate_min`` has no term.
    Its form is strictly proportional to n, so no one base satisfies all
    three and choosing one is choosing WHERE to be right. The choice is a
    SINGLE anchor, job 79228f03, because its 5 designs sit closest to this
    curve's own baseline of 8: a design correction of 0.625, the nearest to
    1 of the three (the others are 0.25 and 3.125). It is NOT the nearest
    in target SIZE and an earlier draft of this docstring said it was --
    job 816fc4a9 at 115 aa is 5 from the 120 aa reference where this one is
    9, and needs the smaller size correction of the two (0.95x against
    1.10x). That run is rejected on design count, not size: 2 designs is a
    quarter of the baseline, and the falling rates above are exactly the
    low-n end a strictly proportional form gets most wrong. The other two
    are treated as residuals to disclose (check 2) and never as
    corroboration.

    ``runtime_alpha`` is NOT pinned and CANNOT be calibrated from these
    runs. That is check (4) below rather than a claim in prose: target
    size is confounded with design count across them (the 420 aa run is
    also the 25-design run), and the log-linear fit through all three
    returns a NEGATIVE exponent. Pinning an exponent properly needs two
    target sizes at one design count, which is what
    ``test_proteina_runtime_curve_bends_with_target_size`` in this file
    has and this tool does not.
    """
    import inspect as _inspect
    import math as _math
    import re as _re
    from pathlib import Path as _Path

    from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
    import tools.pxdesign as _pxdesign

    rules = TOOL_RULES["pxdesign"]
    repo = _Path(__file__).resolve().parent.parent

    # 1HEW chain A is COUNTED, not restated: preflight sizes a target on
    # surviving protein residues, and this is the structure job 79228f03
    # ran against. Hen lysozyme is 129 residues, so a copy that disagreed
    # would be the wrong file rather than a new measurement.
    anchor_aa = len({
        line[22:27]
        for line in (repo / "static" / "example" / "1HEW.pdb").read_text().splitlines()
        if line.startswith("ATOM") and line[21:22] == "A"
    })
    assert anchor_aa == 129, f"1HEW chain A counts {anchor_aa}, expected 129"

    # (1) Reproduce the anchor run. This is the ONLY run the constants are
    #     fitted to, so the band is the +/-5% that the single-anchor
    #     bindcraft and proteina checks in this file use, not a wider one.
    #     The shipped constants put job 79228f03 at 7.69 min against a
    #     measured 7.70. Mutating runtime_base_min and re-running this
    #     test: 10.7 and 11.4 pass, 10.6 and 11.5 fail. Only the LOWER edge
    #     is this check's -- the upper one is check (2)'s over-quote
    #     ceiling, which bites at 11.41, below this check's own 11.78. That
    #     is the trade-off working as intended: base cannot be raised to
    #     soften the n=2 under-read without worsening the large-target
    #     over-quote. The OLD base=300 put this run at 206 min, 2575% out.
    anchor_min = 462.0 / 60.0
    est = runtime_estimate_min(rules, target_aa=anchor_aa, num_designs=5)
    residual = abs(est - anchor_min) / anchor_min
    assert residual <= 0.05, (
        f"base={rules.size.runtime_base_min} alpha={rules.size.runtime_alpha} "
        f"baseline={rules.size.runtime_baseline_designs} puts job 79228f03 at "
        f"{est:.2f} min against a measured {anchor_min:.2f} "
        f"({residual:.0%} out)"
    )
    # The anchor clears the estimator's max(5.0, est) floor, so what passed
    # above is the curve and not the floor standing in for it.
    assert est > 5.0

    # (2) The other two runs are NOT fitted, and this pins how wrong they
    #     are so the envelope comment cannot go stale about it. The 420 aa
    #     bound is a CEILING, not an equality: a change that shrinks that
    #     residual passes, and only a regression fails. Asserting the
    #     current value exactly would pin a known defect in place and fail
    #     the day someone fixes it. The n=2 run needs a different shape --
    #     see the mutation note below it.
    #
    #     Do NOT rewrite this as a corroboration of runtime_base_min. An
    #     earlier draft interpolated these two runs in the design-count
    #     dimension and read 12.21 off the line, which is arithmetically
    #     right and means nothing: the two runs sit at 115 and ~420 aa, so
    #     the line crosses a 3.6x target-size gap that ``runtime_base_min``
    #     is defined to exclude. Size-normalising both to 120 aa first --
    #     which needs the very exponent this test refuses to calibrate --
    #     turns the slope NEGATIVE and reads 7.74 instead.
    est_n2 = runtime_estimate_min(rules, target_aa=115, num_designs=2)
    #     A CEILING ON THE RESIDUAL WOULD BE INERT, so this asserts the
    #     mechanism instead. ``runtime_estimate_min`` ends in
    #     max(5.0, est), which caps this run for every base below ~21.1,
    #     so the under-read is 0.405 whatever the constants say: measured
    #     by mutation, ``residual <= 0.41`` passes at base=0.001 AND at
    #     the 300.0 this change removes, i.e. it could not have caught
    #     the defect this test exists for. What IS pinnable is the claim
    #     the _PXDESIGN comment actually makes -- that the FLOOR and not
    #     the curve decides this run. That stops being true above ~21.1.
    #
    #     The 40% below is a LITERAL on purpose. Recomputing the residual
    #     for the message would print whatever the broken constants imply
    #     rather than what the envelope discloses -- at base=300.0 that
    #     reads -745%, a negative under-read, in the one failure this
    #     test exists to produce.
    assert est_n2 == 5.0, (
        f"job 816fc4a9 now estimates {est_n2:.2f} min against a measured "
        f"8.4, instead of resting on the max(5.0) floor. The ~40% "
        f"under-read the _PXDESIGN envelope discloses is a floor "
        f"artefact, and that is no longer what this run is -- re-read "
        f"that comment before changing this"
    )
    over = (runtime_estimate_min(rules, target_aa=420, num_designs=25) - 23.0) / 23.0
    assert over <= 6.9, (
        f"the 25-design worked example is now over-quoted by {over:.0%}; the "
        f"_PXDESIGN comment discloses ~676% (~178 min against a measured "
        f"23.0). If this shrank, update the comment"
    )

    # (3) The baseline is still the form default, read out of the validator
    #     rather than restated here. bindcraft's had stopped tracking its
    #     form silently before #314 re-anchored it -- nothing failed, the
    #     panel just quoted a runtime for a design count no form submits.
    src = _inspect.getsource(_pxdesign.validate)
    m = _re.search(r'form\.get\("num_designs"\)\s*or\s*"(\d+)"', src)
    assert m, (
        "could not find the num_designs fallback in "
        "tools/pxdesign/__init__.py::validate -- if the parse changed, fix "
        "this test rather than deleting it; the drift it guards is silent"
    )
    default_designs = int(m.group(1))
    assert rules.size.runtime_baseline_designs == default_designs, (
        f"runtime_baseline_designs={rules.size.runtime_baseline_designs} but "
        f"validate() defaults num_designs to {default_designs}; the preflight "
        f"panel is quoting a runtime for a design count the form never submits"
    )
    # The form input carries its OWN default and a user submitting the form
    # untouched never reaches the validator's fallback, so both have to
    # agree or the number pinned above describes neither path.
    form = (repo / "templates" / "tools" / "pxdesign_form.html").read_text()
    shown = _re.search(
        r'name="num_designs"[^>]*?pre_value\(\s*pre_fill\s*,\s*"num_designs"\s*,\s*"(\d+)"\s*\)',
        form,
    )
    assert shown and int(shown.group(1)) == default_designs, (
        f"the num_designs input in templates/tools/pxdesign_form.html shows "
        f"{shown.group(1) if shown else 'no parseable'} default while "
        f"validate() falls back to {default_designs}"
    )

    # (4) The exponent is left alone, and this is the evidence for that.
    #     Fitting all three runs log-linearly -- the only fit they support
    #     -- returns a NEGATIVE alpha, i.e. runtime FALLING as the target
    #     grows. That is the confounding between target size and design
    #     count, not a measurement, so alpha stays on the AF2-IG reasoning
    #     it arrived with. Recomputed here rather than asserted in a
    #     comment; it turns into a real signal the moment a run is added.
    #     No change to the shipped constants can fail it -- ``runs`` is
    #     literal and runtime_alpha reaches only the failure message. It
    #     guards the table beside it, which is the one thing a comment
    #     asserting a fit result cannot do for itself.
    runs = [
        (anchor_aa, 5, 462.0 / 60.0),
        (115, 2, 504.0 / 60.0),
        (420, 25, 1380.0 / 60.0),
    ]
    xs = [_math.log(aa / 120.0) for aa, _n, _t in runs]
    ys = [_math.log(t) - _math.log(n / 8.0) for _aa, n, t in runs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    fitted_alpha = (
        sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        / sum((x - mx) ** 2 for x in xs)
    )
    assert fitted_alpha < 0.0, (
        f"the three pilot runs now fit a positive exponent ({fitted_alpha:.2f}); "
        f"if a run was added or corrected, runtime_alpha="
        f"{rules.size.runtime_alpha} may finally be calibratable and this "
        f"test's premise needs re-reading"
    )


def test_rfantibody_runtime_from_two_runs():
    """rfantibody's three runtime constants are solved from its two recorded runs.

    Both runs are at 4 designs:

      * docs/CALIBRATION-WEEK2.md job #1 -- 1JFF chain A, 412 aa, 2489 s
        (41.4833 min).
      * tools/rfantibody/example/result.json, 2026-09-08 -- 4ZQK chain A,
        115 aa, 13.9 min. Its design count and runtime are READ below
        rather than transcribed, so this test cannot drift from the tool
        page. ``"candidate_count": 20`` against ``"total_designs": 4``
        confirms the 5x ProteinMPNN fanout, which is what makes that 4 a
        BACKBONE count.

    Same design count, different target size, so the pair is two equations
    in (base, alpha) and solves exactly: alpha = ln(2.9844)/ln(3.5826) =
    0.8568 and base = 13.9/(115/120)**alpha = 14.4162, shipped rounded to
    0.86 and 14.4 with ``runtime_baseline_designs`` moved 100 -> 4 to carry
    the base's units.

    What the assertions below establish, and what they do not:

      * Both runs reproduce within 5%, computed from whatever the envelope
        holds rather than from literals, so moving one member of the
        base/baseline pair alone goes red. Live residuals are 0.28% and
        0.13%. The band is sized to catch drift and is NOT evidence the
        curve is right: two points fit a two-parameter power law exactly,
        so nothing is held out and there is no cross-validation here. A
        third run at a third target size is what would test the exponent.
      * The alternative is refuted by mutation rather than the choice
        merely confirmed. Reverting runtime_alpha to 1.2 -- with the base
        refitted on the September run, which is the most favourable base
        there is -- misses job #1 by more than 50%. And no
        ``runtime_fixed_min`` rescues it: that term is multiplied by the
        same size_factor as the base
        (shared/pdb_preflight_rules.py::runtime_estimate_min), so it is a
        design-count term that cancels out of a ratio taken at one design
        count, and both runs sit at 4 designs. Swept over a grid, every
        (base, fixed) pair puts the two runs 4.6242x apart at alpha=1.2
        against a measured 2.9844.
      * The September runtime is corroborated by a second record of the
        same run, the example's ``cost_usd`` at rfantibody's own declared
        GPU class. ``gpu_usd_per_second()`` falls back to the A100-80GB
        rate for a string it does not recognise, which is the misreading
        that makes the same charge look 1.44x off, so the lookup is pinned
        and not just the result.

    docs/VALIDATION-LOG.md job e29a462d is deliberately ABSENT: its design
    count is recorded nowhere. The row says "5 ranked candidates", a
    candidate count, and stage 2 runs ProteinMPNN at seqs_per_backbone=5,
    so it reads as ONE backbone, while the May campaign plan in the same
    file says rfantibody was hardcoded to 2. num_designs is the divisor, so
    those readings put its estimate between 5.0 (the floor, exercising no
    curve at all) and 9.50.

    THE TARGET SIZE of the September run, 115 aa for 4ZQK chain A, is the
    one input it does not record itself. It is attested by three real
    Modal runs recorded in shared/pdb_preflight_rules.py -- the "VERIFIED
    on GPU 2026-08-05" notes on _RFDIFFUSION, _BOLTZGEN and _PXDESIGN,
    each returning {A:115, ...}. meta.py's "crystal numbering 18-132"
    agrees with 115 but is a numbering range rather than a count, so it is
    not what is relied on.

    The cost cross-check below uses ``"cost_usd": "1.01"``
    (tools/rfantibody/meta.py EXAMPLE), the CUSTOMER-facing charge -- raw
    GPU cost times WALLET_MARKUP, the convention enforced by
    tests/test_worked_examples.py::TestExampleNumbersComeFromThePayload
    ::test_recorded_cost_is_what_this_tool_would_charge, which skips
    rfantibody only because it needs an example's gpu_seconds and this
    example records none. shared/wallet.py prices A100-40GB at $0.000714/s
    before a WALLET_MARKUP of 1.70, so $1.01 is 832 s = 13.87 min, 0.2%
    from the recorded figure. The body derives that rate from shared.wallet
    rather than quoting it, and tests/test_gpu_class_drift.py
    ::test_wallet_gpu_class_matches_container is what keeps the class the
    charge is divided by equal to the one the container ran on.

    The estimator's max(5.0, est) floor is not pinned here; the assertions
    below reject only a floor that swallows an anchor outright.

    Advisory only: ``runtime_estimate_min`` reaches users through
    shared/pdb_intake.py and templates/components/preflight_panel.html and
    nothing else. Re-verified at this commit -- no wallet, pricing or submit
    gate reads it (``over_hard``/``over_warn`` in
    shared/pdb_preflight.py::_check_size_envelope key on target_aa, never
    runtime).
    """
    import dataclasses as _dataclasses
    import json as _json
    import pathlib as _pathlib

    from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
    import tools.rfantibody as _rfantibody

    rules = TOOL_RULES["rfantibody"]

    # The September anchor is READ from the worked example rather than
    # transcribed, so this test and the tool page cannot drift apart. The
    # fanout assertion is what distinguishes a backbone count from a
    # candidate count -- getting that wrong is what made e29a462d unusable.
    _example = _json.loads(
        (_pathlib.Path(_rfantibody.__file__).parent / "example" / "result.json")
        .read_text(encoding="utf-8")
    )
    _sept_designs = _example["total_designs"]
    _sept_measured_min = float(_example["runtime_minutes"])
    assert _sept_designs == 4, (
        f"tools/rfantibody/example/result.json now records "
        f"{_sept_designs} designs, not 4. The docstring above reasons from "
        f"4 throughout; re-read it before touching this line."
    )
    assert _example["candidate_count"] == 5 * _sept_designs, (
        f"the worked example records {_example['candidate_count']} "
        f"candidates for {_sept_designs} designs, breaking the 5x "
        f"ProteinMPNN fanout that makes total_designs a BACKBONE count. If "
        f"that ratio really changed, the anchor may no longer be a "
        f"num_designs measurement at all."
    )

    # Both runs, one band. The constants were solved from exactly these two
    # points, so reproducing them is arithmetic and not validation -- the
    # 5% is sized to catch drift in the constants, against live residuals
    # of 0.28% and 0.13%.
    _JOB1_MEASURED_MIN = 2489.0 / 60.0
    for target_aa, num_designs, measured_min, lo, hi, job in (
        (412, 4, _JOB1_MEASURED_MIN, 0.95, 1.05,
         "CALIBRATION-WEEK2.md job #1 (1JFF/A)"),
        (115, _sept_designs, _sept_measured_min, 0.95, 1.05,
         "rfantibody/example/result.json (4ZQK/A, 2026-09-08)"),
    ):
        est = runtime_estimate_min(rules, target_aa=target_aa, num_designs=num_designs)
        ratio = est / measured_min
        assert lo <= ratio <= hi, (
            f"base={rules.size.runtime_base_min} "
            f"alpha={rules.size.runtime_alpha} "
            f"baseline={rules.size.runtime_baseline_designs} puts {job} at "
            f"{est:.2f} min against a measured {measured_min:.2f} "
            f"(ratio {ratio:.3f}, band [{lo}, {hi}])"
        )
        assert est > 5.0, (
            f"{job} estimate {est:.2f} is at or under the estimator's "
            f"max(5.0, est) floor, so the band above would be satisfied by "
            f"the floor rather than by the anchor"
        )

    # The bands above pin runtime_base_min and runtime_baseline_designs only
    # as a PAIR, which a literal divisor inside runtime_estimate_min would
    # satisfy just as well. This pins that the estimator still reads the
    # envelope's own count -- double it and the estimate must halve -- and
    # it is written relative to that count so that re-anchoring the pair
    # cannot turn it into a no-op.
    _doubled = _dataclasses.replace(
        rules,
        size=_dataclasses.replace(
            rules.size,
            runtime_baseline_designs=rules.size.runtime_baseline_designs * 2,
        ),
    )
    _full_est = runtime_estimate_min(rules, target_aa=412, num_designs=4)
    _doubled_est = runtime_estimate_min(_doubled, target_aa=412, num_designs=4)
    assert abs(_doubled_est - _full_est / 2.0) < 1e-9, (
        f"doubling runtime_baseline_designs moved the estimate from "
        f"{_full_est:.4f} to {_doubled_est:.4f}, not to "
        f"{_full_est / 2.0:.4f}; runtime_estimate_min no longer divides by "
        f"the envelope's own runtime_baseline_designs, so the bands above "
        f"are vacuous"
    )

    # REVERTING runtime_alpha TO 1.2 MUST FAIL, and the refit is what makes
    # that a statement about alpha rather than about the base: give 1.2 the
    # most favourable base there is -- the one that reproduces the
    # September run exactly -- and job #1 is still missed by more than 50%.
    _alpha_12_base = _sept_measured_min / (115.0 / 120.0) ** 1.2
    _alpha_12 = _dataclasses.replace(
        rules,
        size=_dataclasses.replace(
            rules.size, runtime_alpha=1.2, runtime_base_min=_alpha_12_base
        ),
    )
    _job1_at_alpha_12 = runtime_estimate_min(
        _alpha_12, target_aa=412, num_designs=4
    )
    assert _job1_at_alpha_12 / _JOB1_MEASURED_MIN > 1.5, (
        f"runtime_alpha=1.2 refitted on the September run "
        f"(base={_alpha_12_base:.2f}) now puts job #1 at "
        f"{_job1_at_alpha_12:.2f} min against a measured "
        f"{_JOB1_MEASURED_MIN:.2f}, within 50%. The shipped 0.86 is "
        f"justified by 1.2 being REFUTED by these two runs; if that is no "
        f"longer true, re-read the solve in shared/pdb_preflight_rules.py"
    )

    # AND NO runtime_fixed_min RESCUES IT. That term is multiplied by the
    # same size_factor as the base, so it is a design-count term: at ONE
    # design count it cancels out of the ratio between the two runs, and
    # both of these runs are at 4 designs. Swept over a grid, every
    # (base, fixed) pair returns the same ratio -- the one alpha alone
    # fixes -- against a measured 2.9844.
    _measured_ratio = _JOB1_MEASURED_MIN / _sept_measured_min
    for _base in (14.4, 200.0, 333.3, 1000.0):
        for _fixed in (0.0, 12.28, 100.0, 5000.0):
            _alt = _dataclasses.replace(
                rules,
                size=_dataclasses.replace(
                    rules.size,
                    runtime_base_min=_base,
                    runtime_alpha=1.2,
                    runtime_fixed_min=_fixed,
                ),
            )
            _alt_hi = runtime_estimate_min(_alt, target_aa=412, num_designs=4)
            _alt_lo = runtime_estimate_min(_alt, target_aa=115, num_designs=4)
            assert _alt_lo > 5.0, (
                f"grid point base={_base} fixed={_fixed} floors the 115 aa "
                f"estimate at max(5.0, est), so its ratio is the floor's "
                f"and this sweep would prove nothing there"
            )
            assert abs(_alt_hi / _alt_lo - 4.6242) < 0.001, (
                f"at alpha=1.2 the two runs came out {_alt_hi / _alt_lo:.4f}x "
                f"apart for base={_base} fixed={_fixed}, not 4.6242x. The "
                f"claim that runtime_fixed_min cancels out of a ratio taken "
                f"at one design count rests on runtime_estimate_min "
                f"multiplying it by the same size_factor as the base; if "
                f"that changed, that claim needs re-deriving"
            )
    assert abs(_measured_ratio - 2.9844) < 0.001, (
        f"the two recorded runs are now {_measured_ratio:.4f}x apart, not "
        f"2.9844x, so the solve the envelope quotes no longer describes "
        f"them and the constants need re-deriving"
    )

    # What makes the September run an INDEPENDENT anchor rather than one
    # recorded number: its cost is a second record of the same run, and at
    # rfantibody's own GPU class it reproduces the runtime. Derive the rate
    # instead of quoting it, so the tool's class, the rate card and the
    # markup are all in the loop.
    import tools.rfantibody.meta as _rfantibody_meta
    from shared.wallet import (
        GPU_USD_PER_SECOND,
        WALLET_MARKUP,
        gpu_usd_per_second,
    )

    # gpu_usd_per_second() falls back to the A100-80GB rate for any string
    # it does not recognise, which is exactly the misreading described
    # above -- so pin the lookup, not just the result.
    assert rules.gpu in GPU_USD_PER_SECOND, (
        f"rfantibody declares gpu={rules.gpu!r}, which is not a key in "
        f"shared.wallet.GPU_USD_PER_SECOND, so gpu_usd_per_second() would "
        f"fall back to the A100-80GB default and price this run at the "
        f"wrong class"
    )
    _charge_rate = gpu_usd_per_second(rules.gpu) * float(WALLET_MARKUP)
    _cost_implied_min = (
        float(_rfantibody_meta.EXAMPLE["cost_usd"]) / _charge_rate / 60.0
    )
    _skew = abs(_cost_implied_min - _sept_measured_min) / _sept_measured_min
    assert _skew < 0.05, (
        f"the September run's two records no longer agree: cost_usd "
        f'{_rfantibody_meta.EXAMPLE["cost_usd"]} at {rules.gpu} '
        f"({_charge_rate:.7f}/s incl. {WALLET_MARKUP}x markup) implies "
        f"{_cost_implied_min:.2f} min against a recorded "
        f"{_sept_measured_min} ({_skew:.1%} apart). Either the rate card "
        f"moved since 2026-09-08 -- in which case pin the rate that was "
        f"live then -- or one of the two records is wrong, and the "
        f"docstring above calls this run independently corroborated."
    )


def test_rfdiffusion_baseline_is_not_the_form_default():
    """rfdiffusion anchors at 100 designs while its form defaults to 4.

    That gap looks like a bug and is not one. ``runtime_baseline_designs``
    is a divisor, and the only rule is that it carries the same units as
    the ``runtime_base_min`` sitting beside it. rfdiffusion's base was
    derived per 100 designs, so re-pointing the baseline at the form
    default without moving the base re-scales its panel by 25x. Neither
    convention is the house style; the PAIRING is the invariant --
    bindcraft's pair moved 300 -> 40 with 10 -> 4, and rfantibody's moved
    200 -> 14.4 with 100 -> 4, each on its own measured runs.

    The validator fallback is read from source rather than assumed,
    because the premise here is that the form still defaults to 4: if it
    ever moved to 100 the two surfaces would agree by accident and this
    test would be asserting nothing.

    boltzgen is the last envelope inheriting
    ``SizeEnvelope.runtime_baseline_designs`` implicitly, so its effective
    value is pinned here too -- nothing else now catches a change to that
    default. It gets no residual pin: its anchor is a sampling batch on a
    published rate rather than a measured run, and its form field is
    ``budget`` rather than ``num_designs``. rfdiffusion's residuals are
    pinned separately, by tests/test_pdb_preflight.py::
    test_rfdiffusion_runtime_curve_reproduces_both_recorded_runs.
    """
    import dataclasses as _dataclasses
    import inspect as _inspect
    import re as _re

    from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
    import tools.rfdiffusion as _rfdiffusion

    # The path is written out rather than interpolated. An f-string path is
    # invisible to tests/test_code_citations_resolve.py
    # ::test_every_code_citation_resolves_to_a_real_symbol -- its token stops
    # at the brace -- so an interpolated citation is unresolvable where a
    # literal one goes red if validate() is ever renamed.
    cite = "tools/rfdiffusion/__init__.py::validate"
    rules = TOOL_RULES["rfdiffusion"]
    src = _inspect.getsource(_rfdiffusion.validate)
    m = _re.search(r'form\.get\("num_designs"\)\s*or\s*"(\d+)"', src)
    assert m, (
        f"could not find the num_designs fallback in {cite} -- if the parse "
        f"changed, fix this test rather than deleting it; it is the premise "
        f"for why runtime_baseline_designs may differ from the form default"
    )
    form_default = int(m.group(1))
    assert form_default == 4, (
        f"{cite} now defaults num_designs to {form_default}, not 4. The "
        f"docstring above argues from a form default of 4 throughout; "
        f"update it with this line. A bare != assertion would have let this "
        f"drift green."
    )
    assert rules.size.runtime_baseline_designs == 100, (
        f"rfdiffusion runtime_baseline_designs is "
        f"{rules.size.runtime_baseline_designs}, not 100. If this was "
        f"changed to track the form default of {form_default} -- as "
        f"bindcraft's and rfantibody's legitimately do -- then "
        f"runtime_base_min has to move with it, and "
        f"test_rfdiffusion_runtime_curve_reproduces_both_recorded_runs is "
        f"where the pair is measured."
    )
    assert form_default != rules.size.runtime_baseline_designs, (
        f"{cite} now defaults num_designs to {form_default}, which equals "
        f"runtime_baseline_designs. This test assumes they differ ON "
        f"PURPOSE; re-read its docstring before relaxing anything here."
    )

    assert TOOL_RULES["boltzgen"].size.runtime_baseline_designs == 100, (
        "boltzgen inherits SizeEnvelope.runtime_baseline_designs; a change "
        "to that default silently re-scales its runtime estimate"
    )

    # Everything above pins a FIELD. This pins that the estimator still
    # READS it on the tool that only inherits it: rewriting
    # runtime_estimate_min's design_factor divisor as a literal 100 would
    # leave every assertion above green while silently re-scaling bindcraft
    # and rfantibody (4) by 25x and pxdesign and proteina (8) by 12.5x.
    _boltzgen = TOOL_RULES["boltzgen"]
    _half = _dataclasses.replace(
        _boltzgen,
        size=_dataclasses.replace(
            _boltzgen.size, runtime_baseline_designs=50
        ),
    )
    _full_est = runtime_estimate_min(_boltzgen, target_aa=412, num_designs=4)
    _half_est = runtime_estimate_min(_half, target_aa=412, num_designs=4)
    assert abs(_half_est - 2 * _full_est) < 1e-9, (
        f"halving boltzgen's runtime_baseline_designs moved the estimate "
        f"from {_full_est:.4f} to {_half_est:.4f}, not to "
        f"{2 * _full_est:.4f}; runtime_estimate_min no longer divides by "
        f"the envelope's own runtime_baseline_designs, so the assertions "
        f"above are vacuous"
    )


def test_rfdiffusion_runtime_curve_reproduces_both_recorded_runs():
    """Pin rfdiffusion's two-term curve to the split it was derived from.

    ``tools/rfdiffusion/meta.py`` records the tool's cost as a fixed
    diffusion + MPNN stage plus a per-design AlphaFold2 re-score, and that
    decomposition is what ``shared/pdb_preflight_rules.py``'s
    ``_RFDIFFUSION`` envelope encodes -- ``runtime_fixed_min`` is the fixed
    stage and ``runtime_base_min`` the per-design one, both normalised to the
    estimator's 120 aa pivot. Every number below is READ out of meta.py
    rather than transcribed, because the whole point of the re-anchor is
    that this file and that one stop drifting apart: #240 (``b692593``)
    measured 277.5 s/design, carried it into the chunking and cost path, and
    left this envelope quoting ~11 min for a job that takes 37.

    FOUR things are pinned, and only the third is the anchor.

    1. meta.py's split reproduces meta.py's own recorded job. ``700 + 190*8
       = 2220`` GPU-s for job 25471e07 exactly. If that stops holding, the
       model this envelope is derived from has moved and the constants below
       are stale by construction -- catching that HERE is the point, because
       nothing else in the repo reads that sentence.

    2. The two design counts the form page advertises are the two the
       seconds sentence covers, and the advertised band brackets them. The
       band is the rounded presentation of the same two numbers (~24.3 min
       shown as 25, ~37.0 shown inside a 40 ceiling), so it is checked
       against the seconds it was derived from rather than against the
       curve: asserting the curve lands inside the rounded text would pin
       meta.py's rounding as a constraint on the estimator.

    3. The curve reproduces BOTH recorded ends within 5%. It lands 0.014%
       out at each, because the constants are that same split divided
       through -- so this is ONE anchor expressed twice, NOT two independent
       confirmations, and the band is the +/-5% that
       ``test_bindcraft_runtime_curve_reproduces_its_one_measured_run``
       uses for its single anchor rather than the looser +/-10% reserved
       for a band covering genuinely separate points. Both ends sit far
       above the estimator's ``max(5.0, est)`` floor, so neither can be
       satisfied by the floor instead of the anchor.

       ``runtime_alpha`` is NOT pinned here and cannot be. Both ends are the
       SAME target (4ZQK chain A, ~115 aa), 4.2% off the 120 aa pivot, so
       ``(115/120)**alpha`` barely moves over any plausible range. Two
       design counts measure the design axis, not the size axis. Treat size
       scaling as unmeasured for this tool.

    4. All three constants are load-bearing, proved by mutation rather than
       asserted. A two-term fit has an obvious failure mode a residual check
       alone will not catch: constants that trade off against each other and
       still pass at one point. Moving each in turn must break the fit.

    The form default is checked too. ``runtime_baseline_designs`` stays at
    100 (pinned as a value by
    ``test_rfdiffusion_baseline_is_not_the_form_default``) and
    the form defaults to 4, so the estimate a default submit shows IS one of
    the two ends pinned here -- which is only true while that default is one
    of the advertised counts.
    """
    import dataclasses as _dc
    import inspect as _inspect
    import re as _re

    from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
    import tools.rfdiffusion as _rfdiffusion
    import tools.rfdiffusion.meta as _meta

    rules = TOOL_RULES["rfdiffusion"]

    # meta.py's runtime note is a comment, so flow it into plain text before
    # reading numbers out of it -- the figures wrap across comment lines.
    _src = _inspect.getsource(_meta)
    _flowed = " ".join(
        _re.sub(r"^\s*#\s?", "", line) for line in _src.split("\n")
    )

    def _one(pattern, what):
        m = _re.search(pattern, _flowed)
        assert m, (
            f"could not read {what} out of tools/rfdiffusion/meta.py. If that "
            f"note was reworded, fix this parse rather than deleting the "
            f"test: the envelope's constants are derived from it and nothing "
            f"else in the repo reads it."
        )
        return m

    # (1) The split reproduces the recorded job.
    _m = _one(
        r"fixed ~(\d+) s of diffusion \+ MPNN plus ~(\d+) s per design",
        "the fixed + per-design split",
    )
    fixed_s, per_design_s = int(_m.group(1)), int(_m.group(2))
    recorded_s = int(
        _one(r"ran (\d+)\s+GPU-seconds", "the recorded job").group(1)
    )
    four_s = int(
        _one(r"four designs is ~(\d+) s", "the 4-design figure").group(1)
    )
    assert fixed_s + per_design_s * 8 == recorded_s, (
        f"meta.py models the job as {fixed_s} s fixed + {per_design_s} s per "
        f"design, which puts 8 designs at "
        f"{fixed_s + per_design_s * 8} s against the {recorded_s} GPU-s it "
        f"records for job 25471e07. The envelope below is derived from that "
        f"split, so it is stale the moment the split stops fitting."
    )
    assert fixed_s + per_design_s * 4 == four_s, (
        f"the same split puts 4 designs at {fixed_s + per_design_s * 4} s "
        f"against the {four_s} s meta.py states"
    )

    # (2) The advertised band is the rounded presentation of those seconds.
    band = _meta.preset_runtime_rows[0]["runtime"]
    nums = [int(x) for x in _re.findall(r"\d+", band)]
    assert len(nums) == 4, (
        f"preset_runtime_rows[0]['runtime'] is {band!r}, which is not the "
        f"'LO to HI min (A to B designs)' shape this check assumes"
    )
    lo_min, hi_min, lo_designs, hi_designs = nums
    assert (lo_designs, hi_designs) == (4, 8), (
        f"the form page advertises {lo_designs} to {hi_designs} designs "
        f"while the runtime note covers 4 and 8; one of the two moved"
    )
    assert lo_min in (four_s // 60, -(-four_s // 60)), (
        f"the page advertises a {lo_min} min floor while its own note puts "
        f"4 designs at {four_s / 60.0:.2f} min"
    )
    assert hi_min >= -(-recorded_s // 60), (
        f"the page advertises a {hi_min} min ceiling under the "
        f"{recorded_s / 60.0:.2f} min its own note records for 8 designs"
    )

    # (3) The curve reproduces both ends. 115 aa is 4ZQK chain A, the target
    #     both runs used.
    def _resid(env, n, measured_s):
        est = runtime_estimate_min(
            _dc.replace(rules, size=env), target_aa=115, num_designs=n
        )
        return est, abs(est - measured_s / 60.0) / (measured_s / 60.0)

    for n, measured_s in ((lo_designs, four_s), (hi_designs, recorded_s)):
        est, residual = _resid(rules.size, n, measured_s)
        assert residual <= 0.05, (
            f"fixed={rules.size.runtime_fixed_min} "
            f"base={rules.size.runtime_base_min} "
            f"alpha={rules.size.runtime_alpha} "
            f"baseline={rules.size.runtime_baseline_designs} puts the "
            f"{n}-design run at {est:.2f} min against a recorded "
            f"{measured_s / 60.0:.2f} ({residual:.1%} out)"
        )
        assert est > 5.0, (
            f"the {n}-design estimate {est:.2f} is at or under the "
            f"estimator's max(5.0, est) floor, so the band above would be "
            f"satisfied by the floor rather than by the anchor"
        )

    # (4) Each constant is load-bearing. Move one, the fit must break at one
    #     end or the other -- a two-term model that still passed both ends
    #     after a constant moved would mean the two terms were absorbing each
    #     other and the residuals above proved nothing.
    for field, value in (
        ("runtime_fixed_min", 0.0),
        ("runtime_base_min", rules.size.runtime_base_min * 2),
        ("runtime_baseline_designs", 4),
    ):
        broken = _dc.replace(rules.size, **{field: value})
        worst = max(
            _resid(broken, n, s)[1]
            for n, s in ((lo_designs, four_s), (hi_designs, recorded_s))
        )
        assert worst > 0.05, (
            f"setting {field}={value} still fits both recorded runs to "
            f"{worst:.1%}, so the assertions above are not actually pinning "
            f"it and the two terms are absorbing each other"
        )

    # The default submit lands on one of the two pinned ends.
    _fallback = _re.search(
        r'form\.get\("num_designs"\)\s*or\s*"(\d+)"',
        _inspect.getsource(_rfdiffusion.validate),
    )
    assert _fallback, (
        "could not find the num_designs fallback in "
        "tools/rfdiffusion/__init__.py::validate -- if the parse changed, "
        "fix this test rather than deleting it; the drift it guards is silent"
    )
    assert int(_fallback.group(1)) in (lo_designs, hi_designs), (
        f"validate() defaults num_designs to {_fallback.group(1)}, which is "
        f"neither of the {lo_designs}/{hi_designs} design counts this test "
        f"pins, so a default submit now shows an estimate nothing here "
        f"covers"
    )


def test_zero_fixed_term_leaves_every_other_envelope_bit_identical():
    """The new fixed term must be an exact no-op wherever it is 0.0.

    ``runtime_fixed_min`` was added for rfdiffusion alone. Folding it into
    the estimator as ``(fixed + base * design_factor) * size_factor`` is the
    same arithmetic but RE-ASSOCIATES the multiplication, and that form was
    executed against this grid: it moves the last bit at 36 of the 405
    points below, on tools this change is not supposed to touch at all.
    ``shared/pdb_preflight_rules.py::runtime_estimate_min`` therefore adds
    it as a separate term, where ``+ 0.0`` is an exact IEEE-754 identity.

    This asserts that by recomputing the OLD expression, spelled out, and
    demanding exact equality rather than closeness. Approximate equality
    would pass against the re-associated form and defeat the whole check.
    """
    from shared.pdb_preflight_rules import (
        SizeEnvelope,
        TOOL_RULES,
        runtime_estimate_min,
    )

    assert SizeEnvelope.runtime_fixed_min == 0.0, (
        "SizeEnvelope.runtime_fixed_min no longer defaults to 0.0, so a new "
        "envelope that does not mention it silently gets a fixed stage"
    )

    with_fixed = sorted(
        slug for slug, r in TOOL_RULES.items() if r.size.runtime_fixed_min
    )
    assert with_fixed == ["rfdiffusion"], (
        f"envelopes carrying a fixed term are now {with_fixed}. That is a "
        f"legitimate change, but the module header and SizeEnvelope docstring "
        f"in shared/pdb_preflight_rules.py both say rfdiffusion is the only "
        f"one -- update them, then update this assertion."
    )

    grid = [
        (aa, n)
        for aa in (1, 30, 61, 115, 120, 200, 331, 500, 720)
        for n in (1, 2, 3, 4, 8, 16, 100, 137, 1000)
    ]
    for slug, rules in TOOL_RULES.items():
        if rules.size.runtime_fixed_min:
            continue
        for aa, n in grid:
            size_factor = (aa / 120.0) ** rules.size.runtime_alpha
            design_factor = n / rules.size.runtime_baseline_designs
            was = max(
                5.0,
                rules.size.runtime_base_min * size_factor * design_factor,
            )
            now = runtime_estimate_min(rules, target_aa=aa, num_designs=n)
            assert now == was, (
                f"{slug} at {aa} aa x {n} designs now estimates {now!r} "
                f"where the pre-fixed-term expression gives {was!r}. "
                f"runtime_fixed_min is 0.0 here, so the only way this moves "
                f"is a re-associated product."
            )
