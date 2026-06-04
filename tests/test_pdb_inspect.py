"""Tests for ``shared.pdb_inspect`` — pre-flight upload inspection.

Bug 9 follow-on: tools-hub rejects obvious garbage and validates user
hotspots BEFORE create_job debits credits. The Kendrew docker pipelines
do the heavy normalization on the server side; tools-hub just gates.

Synthetic PDB strings; no network access needed.
"""
from __future__ import annotations

import pytest

from shared.pdb_inspect import (
    InspectionReport,
    MAX_INSPECT_BYTES,
    inspect_pdb_bytes,
    summarize_for_log,
    validate_hotspots,
    validate_target_chain,
)


# ---------------------------------------------------------------------------
# Fixtures (synthetic PDB strings)
# ---------------------------------------------------------------------------

CLEAN_TWO_RES_PDB = b"""\
HEADER    CLEAN
ATOM      1  N   ALA A  20       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A  20       2.000   1.000   1.000  1.00 10.00           C
ATOM      3  C   ALA A  20       3.000   1.000   1.000  1.00 10.00           C
ATOM      4  O   ALA A  20       3.000   2.000   1.000  1.00 10.00           O
ATOM      5  N   GLY A  21       4.000   1.000   1.000  1.00 10.00           N
ATOM      6  CA  GLY A  21       5.000   1.000   1.000  1.00 10.00           C
ATOM      7  C   GLY A  21       6.000   1.000   1.000  1.00 10.00           C
ATOM      8  O   GLY A  21       6.000   2.000   1.000  1.00 10.00           O
END
"""

PROTEIN_PLUS_NAG_LIGAND_PDB = b"""\
HEADER    LYSOZYME PLUS LIGAND
ATOM      1  N   ALA A  20       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A  20       2.000   1.000   1.000  1.00 10.00           C
ATOM      3  C   ALA A  20       3.000   1.000   1.000  1.00 10.00           C
ATOM      4  O   ALA A  20       3.000   2.000   1.000  1.00 10.00           O
ATOM      5  N   GLY A  21       4.000   1.000   1.000  1.00 10.00           N
ATOM      6  CA  GLY A  21       5.000   1.000   1.000  1.00 10.00           C
ATOM      7  C   GLY A  21       6.000   1.000   1.000  1.00 10.00           C
ATOM      8  O   GLY A  21       6.000   2.000   1.000  1.00 10.00           O
HETATM    9  C1  NAG B 200       9.000   9.000   9.000  1.00 10.00           C
HETATM   10  C2  NAG B 200      10.000   9.000   9.000  1.00 10.00           C
END
"""

PROTEIN_WITH_WATERS_PDB = b"""\
HEADER    PROTEIN WITH WATERS
ATOM      1  N   ALA A   1       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A   1       2.000   1.000   1.000  1.00 10.00           C
ATOM      3  C   ALA A   1       3.000   1.000   1.000  1.00 10.00           C
ATOM      4  O   ALA A   1       3.000   2.000   1.000  1.00 10.00           O
HETATM    5  O   HOH A 101      10.000  10.000  10.000  1.00 30.00           O
HETATM    6  O   HOH A 102      11.000  10.000  10.000  1.00 30.00           O
END
"""

LIGAND_ONLY_PDB = b"""\
HEADER    LIGAND ONLY
HETATM    1  C1  NAG B 200       9.000   9.000   9.000  1.00 10.00           C
HETATM    2  C2  NAG B 200      10.000   9.000   9.000  1.00 10.00           C
END
"""

NMR_PDB = b"""\
HEADER    NMR TEST
MODEL        1
ATOM      1  N   ALA A   1       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA  ALA A   1       2.000   1.000   1.000  1.00 10.00           C
ATOM      3  C   ALA A   1       3.000   1.000   1.000  1.00 10.00           C
ATOM      4  O   ALA A   1       3.000   2.000   1.000  1.00 10.00           O
ENDMDL
MODEL        2
ATOM      1  N   ALA A   1       9.000   9.000   9.000  1.00 10.00           N
ATOM      2  CA  ALA A   1       9.000   9.000   9.000  1.00 10.00           C
ATOM      3  C   ALA A   1       9.000   9.000   9.000  1.00 10.00           C
ATOM      4  O   ALA A   1       9.000   9.000   9.000  1.00 10.00           O
ENDMDL
END
"""


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------

def test_clean_pdb_passes():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    assert report.ok
    assert report.error is None
    assert report.chain_ids() == ["A"]
    assert report.total_standard_residues == 2
    assert report.chain("A").min_resnum == 20
    assert report.chain("A").max_resnum == 21


def test_returns_dataclass():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    assert isinstance(report, InspectionReport)


# ---------------------------------------------------------------------------
# Tests: rejection paths (the user-facing point of this module)
# ---------------------------------------------------------------------------

def test_empty_file_rejected():
    report = inspect_pdb_bytes(b"")
    assert not report.ok
    assert "empty" in report.error.lower()


def test_huge_file_rejected_without_parsing():
    big = b"X" * (MAX_INSPECT_BYTES + 1)
    report = inspect_pdb_bytes(big)
    assert not report.ok
    assert "too large" in report.error.lower()


def test_no_protein_residues_rejected():
    """Pure-ligand input is the worst-case spam: nothing for the GPU to design against."""
    report = inspect_pdb_bytes(LIGAND_ONLY_PDB)
    assert not report.ok
    assert "no standard protein residues" in report.error.lower()


def test_garbled_input_rejected_gracefully():
    """A non-PDB bytestream should produce a friendly error, not a stack trace."""
    report = inspect_pdb_bytes(b"this is not a pdb file at all\n")
    # Biopython's PDB parser is permissive — it may parse this as zero-residue.
    # Either we reject for "no protein residues" or for "could not parse".
    # Both are acceptable user-facing failures.
    assert not report.ok
    assert report.error  # non-empty


# ---------------------------------------------------------------------------
# Tests: inspection of complex inputs
# ---------------------------------------------------------------------------

def test_ligand_chain_listed_separately():
    """The 1HEW failure case: chain A protein + chain B NAG. Both surface."""
    report = inspect_pdb_bytes(PROTEIN_PLUS_NAG_LIGAND_PDB)
    assert report.ok
    # Chain A has protein, chain B has only ligand.
    assert "A" in report.chain_ids()
    chain_a = report.chain("A")
    assert chain_a.standard_residue_count == 2


def test_waters_counted_separately_from_hetatm():
    report = inspect_pdb_bytes(PROTEIN_WITH_WATERS_PDB)
    assert report.ok
    chain_a = report.chain("A")
    assert chain_a.water_count == 2
    # HOH should not be in hetatm_resnames (waters are filtered out separately).
    assert "HOH" not in chain_a.hetatm_resnames


def test_multi_model_warning():
    report = inspect_pdb_bytes(NMR_PDB)
    assert report.ok
    assert report.model_count == 2
    assert any("multi-model" in w.lower() or "nmr" in w.lower()
               for w in report.warnings)


# ---------------------------------------------------------------------------
# Tests: altloc detection (rfantibody hcruz@indicasat fix, 2026-06-03)
# ---------------------------------------------------------------------------

ALTLOC_PDB = b"""\
HEADER    ALTLOC
ATOM      1  N   ALA A  20       1.000   1.000   1.000  1.00 10.00           N
ATOM      2  CA AALA A  20       2.000   1.000   1.000  0.50 10.00           C
ATOM      3  CA BALA A  20       2.500   1.500   1.500  0.50 10.00           C
ATOM      4  C   ALA A  20       3.000   1.000   1.000  1.00 10.00           C
ATOM      5  O   ALA A  20       3.000   2.000   1.000  1.00 10.00           O
ATOM      6  N   GLY A  21       4.000   1.000   1.000  1.00 10.00           N
ATOM      7  CA  GLY A  21       5.000   1.000   1.000  1.00 10.00           C
ATOM      8  C   GLY A  21       6.000   1.000   1.000  1.00 10.00           C
ATOM      9  O   GLY A  21       6.000   2.000   1.000  1.00 10.00           O
END
"""


def test_clean_pdb_has_zero_altloc_count():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    assert report.ok
    assert report.altloc_atom_count == 0
    assert not any("alternate-conformation" in w.lower() for w in report.warnings)


def test_altloc_records_counted_and_warned():
    """3IUT/3KKU class structures with altloc A/B/C records caused the
    RFdiffusion "Non-positive determinant" crash for hcruz on 2026-06-03;
    surface the count in warnings so log triage spots it fast.
    """
    report = inspect_pdb_bytes(ALTLOC_PDB)
    assert report.ok
    # Two altloc-letter atom records (CA A, CA B).
    assert report.altloc_atom_count == 2
    assert any("alternate-conformation" in w for w in report.warnings)


def test_summarize_for_log_includes_altloc_count():
    report = inspect_pdb_bytes(ALTLOC_PDB)
    line = summarize_for_log(report)
    assert "altloc=2" in line


# ---------------------------------------------------------------------------
# Tests: target_chain validation
# ---------------------------------------------------------------------------

def test_target_chain_present_returns_none():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    assert validate_target_chain(report, "A") is None


def test_target_chain_missing_returns_error():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    err = validate_target_chain(report, "Z")
    assert err is not None
    assert "Z" in err
    assert "A" in err  # mentions what IS present


def test_target_chain_empty_string_rejected():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    err = validate_target_chain(report, "")
    assert err is not None


# ---------------------------------------------------------------------------
# Tests: hotspot validation
# ---------------------------------------------------------------------------

def test_hotspots_in_range():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)  # chain A, residues 20-21
    in_range, out = validate_hotspots(report, "A", [20, 21])
    assert in_range == [20, 21]
    assert out == []


def test_hotspots_out_of_range_caught():
    """User typed hotspots from the original PDB header but the chain doesn't have those residues."""
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)  # chain A, residues 20-21
    in_range, out = validate_hotspots(report, "A", [20, 100])
    assert in_range == [20]
    assert 100 in out


def test_hotspots_non_integer_caught():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    in_range, out = validate_hotspots(report, "A", ["abc"])
    assert "abc" in out


def test_hotspots_against_missing_chain_all_rejected():
    report = inspect_pdb_bytes(CLEAN_TWO_RES_PDB)
    in_range, out = validate_hotspots(report, "Z", [1, 2, 3])
    assert in_range == []
    assert out == [1, 2, 3]


# ---------------------------------------------------------------------------
# Tests: log helper
# ---------------------------------------------------------------------------

def test_summarize_for_log_one_line():
    report = inspect_pdb_bytes(PROTEIN_PLUS_NAG_LIGAND_PDB)
    line = summarize_for_log(report)
    assert "\n" not in line
    assert "chains=" in line
    assert "protein_res=" in line
