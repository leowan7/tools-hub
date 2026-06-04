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
    HOTSPOTS_REQUIRED,
    MIN_TARGET_RESIDUES,
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
    assert BINDER_DESIGN_TOOLS == frozenset({
        "rfantibody", "rfdiffusion", "bindcraft", "boltzgen",
    })


def test_hotspots_required_for_three_tools():
    assert HOTSPOTS_REQUIRED == frozenset({
        "rfantibody", "rfdiffusion", "bindcraft",
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
