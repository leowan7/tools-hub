"""Secondary-structure assignment guards for Epitope Scout.

assign_dssp() and _assign_ss_by_phi_psi() shipped with no test coverage at
all, which is how scout/scoring.py came to read the amino-acid column of the
Biopython DSSP tuple instead of the secondary-structure column. Nothing
caught it because mkdssp is not installed anywhere the suite runs, so the
DSSP branch never executed.

These tests fake the DSSP object rather than requiring the binary, so the
branch is exercised on every machine.

Evidence: docs/qc/scout-dssp-fallback-measurement.md
"""

import numpy as np
import pytest
from Bio.PDB.Atom import Atom
from Bio.PDB.Chain import Chain
from Bio.PDB.Model import Model
from Bio.PDB.Residue import Residue

from scout import scoring


class _FakeDSSP:
    """Stands in for Bio.PDB.DSSP.DSSP.

    Biopython's tuple layout is
        (dssp_index, amino_acid, secondary_structure, rel_asa, phi, psi, ...)
    so index 1 is the residue letter and index 2 is the SS code.
    """

    def __init__(self, rows):
        # rows: {key: (amino_acid_letter, ss_code)}
        self._d = {
            k: (i + 1, aa, ss, 0.5, -60.0, -45.0, 0, 0.0, 0, 0.0, 0, 0.0, 0, 0.0)
            for i, (k, (aa, ss)) in enumerate(rows.items())
        }

    @property
    def property_keys(self):
        return list(self._d)

    def __getitem__(self, key):
        return self._d[key]


def _fake_dssp_factory(rows):
    def _factory(model, pdb_path, dssp="mkdssp"):
        return _FakeDSSP(rows)

    return _factory


def test_assign_dssp_reads_the_secondary_structure_column(monkeypatch):
    """His/Gly/Ile/Glu in a coil must NOT be called helix/strand.

    H, G, I and E are legal letters in BOTH the amino-acid alphabet and the
    DSSP code alphabet. Reading the wrong column is therefore silent: it
    produces plausible labels that are simply wrong.
    """
    rows = {
        # key            amino acid, real DSSP code
        ("A", (" ", 1, " ")): ("H", "-"),   # His in coil
        ("A", (" ", 2, " ")): ("G", "S"),   # Gly in a bend
        ("A", (" ", 3, " ")): ("I", "T"),   # Ile in a turn
        ("A", (" ", 4, " ")): ("E", "-"),   # Glu in coil
        ("A", (" ", 5, " ")): ("A", "H"),   # Ala in an alpha helix
        ("A", (" ", 6, " ")): ("L", "E"),   # Leu in a beta strand
    }
    monkeypatch.setattr(scoring, "DSSP", _fake_dssp_factory(rows))

    ss_map = scoring.assign_dssp(object(), "unused.pdb")

    assert ss_map[("A", (" ", 1, " "))] == "loop"
    assert ss_map[("A", (" ", 2, " "))] == "loop"
    assert ss_map[("A", (" ", 3, " "))] == "loop"
    assert ss_map[("A", (" ", 4, " "))] == "loop"
    assert ss_map[("A", (" ", 5, " "))] == "helix"
    assert ss_map[("A", (" ", 6, " "))] == "strand"


def test_assign_dssp_maps_every_dssp_code(monkeypatch):
    """G and I are 3-10 and pi helix; B is a beta bridge; P/T/S/'-' are loop."""
    codes = {
        "H": "helix", "G": "helix", "I": "helix",
        "E": "strand", "B": "strand",
        "T": "loop", "S": "loop", "P": "loop", "-": "loop", " ": "loop",
    }
    rows = {
        ("A", (" ", i, " ")): ("A", code)
        for i, code in enumerate(codes, start=1)
    }
    monkeypatch.setattr(scoring, "DSSP", _fake_dssp_factory(rows))

    ss_map = scoring.assign_dssp(object(), "unused.pdb")

    for i, (code, expected) in enumerate(codes.items(), start=1):
        assert ss_map[("A", (" ", i, " "))] == expected, f"DSSP code {code!r}"


def test_assign_dssp_falls_back_when_the_binary_is_missing(monkeypatch, caplog):
    """No mkdssp on PATH (the production reality) must not raise."""
    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)
    sentinel = {("A", (" ", 1, " ")): "helix"}
    monkeypatch.setattr(scoring, "_assign_ss_by_phi_psi", lambda model: sentinel)

    with caplog.at_level("WARNING"):
        assert scoring.assign_dssp(object(), "unused.pdb") is sentinel
    assert "falling back to phi/psi" in caplog.text


def _straight_chain(n=4, collinear=True):
    """Model whose backbone atoms are exactly collinear.

    Collinear backbone atoms give a zero-length cross product inside
    Bio.PDB.vectors.Vector.angle, which divides by zero and emits
    "RuntimeWarning: invalid value encountered in scalar divide".
    """
    model = Model(0)
    chain = Chain("A")
    model.add(chain)
    for i in range(1, n + 1):
        res = Residue((" ", i, " "), "ALA", "")
        for j, name in enumerate(("N", "CA", "C")):
            if collinear:
                coord = np.array([float(i * 3 + j), 0.0, 0.0])
            else:
                coord = np.array([float(i * 3 + j), float(j), 0.0])
            res.add(Atom(name, coord, 0.0, 1.0, " ", name, i * 10 + j, "C"))
        chain.add(res)
    return model


def test_phi_psi_fallback_degenerate_geometry_yields_loop_not_a_crash():
    """A NaN dihedral must not become a spurious helix or strand.

    Biopython clamps the NaN to +/-pi inside Vector.angle, and +/-pi falls
    outside every Ramachandran window, so the residue lands on "loop".
    np.degrees(nan) would also compare False against every bound and land on
    "loop". Either way the failure mode is conservative -- assert that, so a
    future refactor cannot turn it into a confident wrong label.
    """
    ss_map = scoring._assign_ss_by_phi_psi(_straight_chain())
    assert ss_map, "expected one entry per residue"
    assert set(ss_map.values()) == {"loop"}


@pytest.mark.parametrize("build", [
    lambda: Model(0),                                    # no chains
    lambda: _empty_chain_model(),                        # chain with no residues
])
def test_phi_psi_fallback_returns_empty_without_raising(build):
    """The {} outcome is reached by returning {}, never by an exception.

    assign_dssp only logs "Phi/psi fallback also failed" when
    _assign_ss_by_phi_psi RAISES. It does not raise on empty input, so that
    second warning is effectively unreachable and an all-loop score carries
    no log signal.
    """
    assert scoring._assign_ss_by_phi_psi(build()) == {}


def _empty_chain_model():
    model = Model(0)
    model.add(Chain("A"))
    return model


def test_empty_ss_map_scores_every_patch_identically():
    """{} means every patch gets the same ss_score, so the SS term drops out
    of the ranking entirely rather than reordering it."""
    from scout.pipeline import _continuous_ss_score

    model = _straight_chain(n=6, collinear=False)
    residues = list(model["A"].get_residues())
    a = _continuous_ss_score(residues[:3], {})
    b = _continuous_ss_score(residues[3:], {})
    assert a == b == 0.2
