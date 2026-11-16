"""Measured surface-patch figure: shared/surface_patch.py.

The brief's hard requirement is that this degrades to showing NOTHING, never
to an error and never to a zero that reads as a measurement. Most of the tests
below are that contract. The arithmetic tests use a hand-built PDB so the
expected answer is known without trusting the thing under test.
"""

from __future__ import annotations

import pathlib

import pytest

from shared.surface_patch import (
    EXPOSED_ATOM_SASA_A2,
    HYDROPHOBIC_ELEMENTS,
    PATCH_CONTACT_A,
    surface_profile,
)

_REPO = pathlib.Path(__file__).resolve().parent.parent
_REAL_PDB = _REPO / "static" / "example" / "1HEW.pdb"


def _atom(serial: int, name: str, resname: str, chain: str, resseq: int, xyz, element):
    """One fixed-column PDB ATOM record.

    The columns are the format's, not free-form, and they bit once already:
    resName is 18-20 with altLoc at 17, so omitting the altLoc space shifts
    chainID off column 22 and every chain reads as blank. Field positions:
    serial 7-11, name 13-16, altLoc 17, resName 18-20, chainID 22,
    resSeq 23-26, x/y/z 31-38/39-46/47-54, occupancy 55-60, tempFactor 61-66,
    element 77-78.
    """
    x, y, z = xyz
    return (
        f"ATOM  {serial:5d} {name:<4s} {resname:>3s} {chain}{resseq:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}\n"
    )


def _one_residue_pdb(chain="B", element="C", resname="ALA") -> bytes:
    """A single isolated atom: fully exposed, so its SASA is its whole sphere."""
    return (_atom(1, "CB", resname, chain, 1, (0.0, 0.0, 0.0), element) + "END\n").encode()


def _two_far_atoms_pdb(chain="B") -> bytes:
    """Two apolar atoms far enough apart to be two patches, not one."""
    return (
        _atom(1, "CB", "ALA", chain, 1, (0.0, 0.0, 0.0), "C")
        + _atom(2, "CB", "ALA", chain, 2, (50.0, 0.0, 0.0), "C")
        + "END\n"
    ).encode()


def _two_touching_atoms_pdb(chain="B") -> bytes:
    """Two apolar atoms within PATCH_CONTACT_A, so ONE patch."""
    return (
        _atom(1, "CB", "ALA", chain, 1, (0.0, 0.0, 0.0), "C")
        + _atom(2, "CB", "ALA", chain, 2, (PATCH_CONTACT_A - 1.0, 0.0, 0.0), "C")
        + "END\n"
    ).encode()


# ---------------------------------------------------------------------------
# Degradation: None, never an exception, never a misleading zero.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a structure at all",
        b"\x00\x01\x02\x03",
        b"HEADER    truncated right here",
    ],
    ids=["empty", "prose", "binary", "truncated-header"],
)
def test_unreadable_bytes_give_none_not_an_exception(data):
    assert surface_profile(data, ["B"]) is None


def test_no_designed_chain_named_gives_none():
    """An empty or blank chain set is the "we do not know which chain" case."""
    assert surface_profile(_one_residue_pdb(), []) is None
    assert surface_profile(_one_residue_pdb(), ["", "  "]) is None


def test_a_chain_that_is_not_in_the_file_gives_none():
    """Not a zero: nothing was measured, so nothing is reported."""
    assert surface_profile(_one_residue_pdb(chain="B"), ["Z"]) is None


def test_a_structure_with_no_protein_residue_gives_none():
    """Waters and ligands are not a surface we report on."""
    only_water = (
        _atom(1, "O", "HOH", "B", 1, (0.0, 0.0, 0.0), "O") + "END\n"
    ).encode()
    assert surface_profile(only_water, ["B"]) is None


def test_none_is_distinguishable_from_a_measured_zero():
    """The two cases that must not collapse into each other.

    A chain of only polar atoms IS measured and genuinely has no exposed
    apolar atom, so it reports 0.0. Bytes that do not parse report None. If
    these ever became the same value, a page could not tell "flat" from
    "unknown" -- which is the whole point of the degradation rule.
    """
    polar_only = (
        _atom(1, "N", "GLY", "B", 1, (0.0, 0.0, 0.0), "N")
        + _atom(2, "O", "GLY", "B", 1, (1.4, 0.0, 0.0), "O")
        + "END\n"
    ).encode()
    measured = surface_profile(polar_only, ["B"])
    assert measured is not None
    assert measured["largest_patch_a2"] == 0.0
    assert measured["patch_atom_count"] == 0
    assert surface_profile(b"garbage", ["B"]) is None


# ---------------------------------------------------------------------------
# The arithmetic.
# ---------------------------------------------------------------------------

def test_a_lone_apolar_atom_is_one_patch_of_its_own_area():
    got = surface_profile(_one_residue_pdb(element="C"), ["B"])
    assert got is not None
    assert got["patch_atom_count"] == 1
    # A free carbon's sphere at probe 1.4: 4*pi*(1.7+1.4)^2 ~ 121 A^2. Assert
    # the order of magnitude rather than Biopython's exact quadrature.
    assert 100.0 < got["largest_patch_a2"] < 140.0
    # The only atom is the whole exposed apolar surface.
    assert got["largest_patch_a2"] == got["total_apolar_a2"]


def test_touching_atoms_are_one_patch_and_far_atoms_are_two():
    """The clustering rule, stated as the difference it makes.

    Both files hold two identical fully-exposed carbons. Only the distance
    differs, so the summed total is the same either way and the LARGEST patch
    is what separates them: one patch of both atoms, or two of one each.
    """
    near = surface_profile(_two_touching_atoms_pdb(), ["B"])
    far = surface_profile(_two_far_atoms_pdb(), ["B"])
    assert near is not None and far is not None

    assert near["patch_atom_count"] == 2
    assert far["patch_atom_count"] == 1
    assert near["largest_patch_a2"] > far["largest_patch_a2"]
    # Non-vacuity: if the clusterer linked everything, or nothing, one of the
    # two counts above would be wrong -- but also pin that "far" really did
    # see both atoms, so the difference is the clustering and not a lost atom.
    assert far["total_apolar_a2"] > far["largest_patch_a2"]


def test_polar_atoms_are_not_part_of_a_patch():
    """N and O are excluded, so adding them next door changes no area."""
    apolar_only = surface_profile(_one_residue_pdb(element="C"), ["B"])
    with_polar_neighbour = surface_profile(
        (
            _atom(1, "CB", "ALA", "B", 1, (0.0, 0.0, 0.0), "C")
            + _atom(2, "N", "ALA", "B", 2, (3.0, 0.0, 0.0), "N")
            + "END\n"
        ).encode(),
        ["B"],
    )
    assert apolar_only is not None and with_polar_neighbour is not None
    assert with_polar_neighbour["patch_atom_count"] == 1
    assert "N" not in HYDROPHOBIC_ELEMENTS and "O" not in HYDROPHOBIC_ELEMENTS
    # The carbon is shaded a little by the neighbour, so areas differ; what
    # must hold is that the nitrogen contributed no patch atom.
    assert with_polar_neighbour["largest_patch_a2"] <= apolar_only["largest_patch_a2"]


# ---------------------------------------------------------------------------
# The free-binder decision, and the real structure.
# ---------------------------------------------------------------------------

def test_only_the_designed_chains_are_measured():
    """The module's central decision: the OTHER chains are removed first.

    Chain B is measured identically whether or not a chain A sits right on top
    of it. Were the whole complex measured, A would bury B and shrink the
    patch. The two atoms here are 2 A apart, well inside contact, so a
    complex-wide calculation could not return the same number.
    """
    alone = _atom(1, "CB", "ALA", "B", 1, (0.0, 0.0, 0.0), "C") + "END\n"
    smothered = (
        _atom(1, "CB", "ALA", "B", 1, (0.0, 0.0, 0.0), "C")
        + _atom(2, "CB", "ALA", "A", 1, (2.0, 0.0, 0.0), "C")
        + "END\n"
    )
    got_alone = surface_profile(alone.encode(), ["B"])
    got_smothered = surface_profile(smothered.encode(), ["B"])
    assert got_alone is not None and got_smothered is not None
    assert got_alone["largest_patch_a2"] == got_smothered["largest_patch_a2"]


def test_residue_sasa_is_one_entry_per_residue_in_chain_order():
    data = (
        _atom(1, "CB", "ALA", "B", 1, (0.0, 0.0, 0.0), "C")
        + _atom(2, "CB", "ALA", "B", 2, (10.0, 0.0, 0.0), "C")
        + _atom(3, "O", "HOH", "B", 3, (20.0, 0.0, 0.0), "O")
        + "END\n"
    ).encode()
    got = surface_profile(data, ["B"])
    assert got is not None
    # Two protein residues, and the water is not one of them.
    assert list(got["residue_sasa"]) == ["B"]
    assert len(got["residue_sasa"]["B"]) == 2
    assert all(v > 0 for v in got["residue_sasa"]["B"])


_COMMITTED_STRUCTURES = [
    (_REPO / "static" / "example" / "1HEW.pdb", "A"),
    (_REPO / "static" / "example" / "3ave_igg1_fc_dimer.pdb", "A"),
    (_REPO / "static" / "example" / "3s7g_fc_ab.pdb", "A"),
    (_REPO / "tests" / "fixtures" / "rfd4" / "1igt_fv_trimmed.pdb", "A"),
]


@pytest.mark.parametrize(
    "path,chain",
    [pytest.param(p, c, id=p.name) for p, c in _COMMITTED_STRUCTURES],
)
def test_the_patch_does_not_percolate_on_any_committed_structure(path, chain):
    """The property that makes the figure mean anything.

    A protein's exposed apolar atoms form a connected network at a loose
    contact cutoff, so with a low exposure floor the "largest patch" grows to
    cover the entire surface and silently becomes total-exposed-apolar-area
    under another name. Measured at an exposure floor of 1 A^2 this reaches
    98-100% of the total on two of these four structures; the floor in
    EXPOSED_ATOM_SASA_A2 is set where it is to stop that, and this test is
    what fails if it is loosened back.

    The 0.5 bound is deliberately far above the 0.05-0.14 the four actually
    produce: it is a percolation alarm, not a pin on the values.
    """
    if not path.exists():
        pytest.skip(f"fixture missing: {path.name}")
    got = surface_profile(path.read_bytes(), [chain])
    assert got is not None, f"{path.name} chain {chain} did not measure"
    assert got["total_apolar_a2"] > 0, "no exposed apolar area at all"
    fraction = got["largest_patch_a2"] / got["total_apolar_a2"]
    assert fraction < 0.5, (
        f"{path.name} chain {chain}: the largest patch is {fraction:.0%} of the "
        f"{got['total_apolar_a2']} A^2 exposed apolar surface "
        f"({got['largest_patch_a2']} A^2, {got['patch_atom_count']} atoms). "
        "The apolar surface has percolated into one cluster, so this figure is "
        "a total and not a patch. Check EXPOSED_ATOM_SASA_A2 and "
        "PATCH_CONTACT_A."
    )


@pytest.mark.skipif(not _REAL_PDB.exists(), reason="fixture missing")
def test_a_real_structure_gives_a_plausible_patch():
    """Floor against a module that returns something tiny for everything."""
    got = surface_profile(_REAL_PDB.read_bytes(), ["A"])
    assert got is not None
    assert got["patch_atom_count"] > 1
    # Lysozyme is ~129 residues; its largest apolar patch is tens to hundreds
    # of A^2, and its exposed apolar total is well under its ~8500 A^2 total
    # SASA. Deliberately loose -- this is a sanity band, not a reference value.
    assert 20.0 < got["largest_patch_a2"] < 3000.0
    assert got["largest_patch_a2"] <= got["total_apolar_a2"]
    assert len(got["residue_sasa"]["A"]) > 100


def test_the_exposure_floor_actually_excludes_something():
    """Non-vacuity for EXPOSED_ATOM_SASA_A2.

    A buried atom must not seed a patch. The central carbon here is caged by
    six neighbours at 2 A, so its own SASA is driven under the floor while the
    cage atoms stay exposed -- if the floor were ignored, the patch atom count
    would include it.
    """
    lines = [_atom(1, "CB", "ALA", "B", 1, (0.0, 0.0, 0.0), "C")]
    for i, (dx, dy, dz) in enumerate(
        [(2, 0, 0), (-2, 0, 0), (0, 2, 0), (0, -2, 0), (0, 0, 2), (0, 0, -2)], start=2
    ):
        lines.append(_atom(i, "CB", "ALA", "B", i, (dx, dy, dz), "C"))
    got = surface_profile(("".join(lines) + "END\n").encode(), ["B"])
    assert got is not None
    caged = got["residue_sasa"]["B"][0]
    assert caged <= EXPOSED_ATOM_SASA_A2, (
        f"the caged atom has SASA {caged}, above the floor "
        f"{EXPOSED_ATOM_SASA_A2} -- this fixture no longer tests the floor"
    )
    assert got["patch_atom_count"] == 6


def test_waters_in_the_designed_chain_do_not_bury_the_surface():
    """A crystallographic water must not occlude the surface being measured.

    ShrakeRupley occludes with every atom in the model it is given, so a HOH
    left in the designed chain buries the protein under it -- while being the
    very solvent this figure measures accessibility to. static/example/1HEW.pdb
    carries 103 HOH records in chain A.

    The assertion is INVARIANCE, not a pinned number: stripping the waters out
    of the file by hand must give the same answer as handing the file over
    whole, because surface_profile drops them itself. Before that fix the two
    differed -- 1277.67 against 2032.02 total apolar area, and 99.03 against
    114.72 for the patch.
    """
    whole = _REAL_PDB.read_bytes()
    stripped = b"\n".join(
        line for line in whole.splitlines()
        if not line.startswith(b"HETATM")
    ) + b"\nEND\n"

    assert b"HOH" in whole, "fixture no longer carries waters; this test is vacuous"

    got_whole = surface_profile(whole, ["A"])
    got_stripped = surface_profile(stripped, ["A"])
    assert got_whole is not None and got_stripped is not None

    for key in ("largest_patch_a2", "total_apolar_a2", "patch_atom_count"):
        assert got_whole[key] == got_stripped[key], (
            f"{key} differs with and without the waters "
            f"({got_whole[key]} vs {got_stripped[key]}), so HETATM residues "
            "are still occluding the measured surface."
        )
    assert got_whole["residue_rsa"] == got_stripped["residue_rsa"]
    assert got_whole["residue_sasa"] == got_stripped["residue_sasa"]


def test_a_ligand_only_chain_is_not_measured_as_protein():
    """Dropping non-protein residues must not turn an empty chain into a zero."""
    water_only = (
        "HETATM    1  O   HOH A   1      10.000  10.000  10.000  1.00  0.00           O\n"
        "HETATM    2  O   HOH A   2      14.000  10.000  10.000  1.00  0.00           O\n"
        "END\n"
    ).encode()
    assert surface_profile(water_only, ["A"]) is None
