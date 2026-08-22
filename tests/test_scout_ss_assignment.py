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

from pathlib import Path

import numpy as np
import pytest
from Bio.PDB.Atom import Atom
from Bio.PDB.Chain import Chain
from Bio.PDB.Model import Model
from Bio.PDB.Residue import Residue

from scout import scoring
from scout.sasa import STANDARD_AA


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

    ss_map, method = scoring.assign_dssp(object(), "unused.pdb")

    assert method == "dssp"
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

    ss_map, method = scoring.assign_dssp(object(), "unused.pdb")

    assert method == "dssp"
    for i, (code, expected) in enumerate(codes.items(), start=1):
        assert ss_map[("A", (" ", i, " "))] == expected, f"DSSP code {code!r}"


def test_assign_dssp_falls_back_when_the_binary_is_missing(monkeypatch, caplog):
    """No mkdssp on PATH (the production reality) must not raise.

    ``object()`` has no get_chains, so the pydssp branch raises internally
    and is skipped -- which is the point: a broken branch must fall through
    to the next one rather than propagate.
    """
    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)
    sentinel = {("A", (" ", 1, " ")): "helix"}
    monkeypatch.setattr(scoring, "_assign_ss_by_phi_psi", lambda model: sentinel)

    with caplog.at_level("WARNING"):
        ss_map, method = scoring.assign_dssp(object(), "unused.pdb")
    assert ss_map is sentinel
    assert method == "phi_psi"
    assert "falling back to in-process assignment" in caplog.text
    assert "pydssp SS assignment failed" in caplog.text


def test_pydssp_refuses_partial_assignment_so_ss_method_cannot_lie(monkeypatch):
    """A partial pydssp map would make ss_method report a branch that did not run.

    assign_dssp only falls through when the map is ENTIRELY empty. So if
    pydssp skipped one chain (or one residue) and labelled the rest, the run
    would be stamped ss_method="pydssp" while run_pipeline scored the skipped
    chain wholly on the "loop" floor -- ss_map.get(key, "loop"). Whole-model
    all-or-nothing is what makes the single per-run column truthful, so it is
    asserted here rather than assumed.

    TWO-chain fixture, deliberately. On a single-chain model "skip the chain"
    and "abort the map" both yield {} and the assertion cannot tell them
    apart -- this test used 1HEW (one protein chain; B is the NAG) and passed
    against a `continue` mutant of the very guard it names.
    """
    import copy

    model = _example_model("3s7g_fc_ab.pdb")
    assert len(scoring._assign_ss_by_pydssp(model)) == 130, "fixture sanity"

    maimed = copy.deepcopy(model)
    victim = next(r for r in maimed["A"].get_residues() if "O" in r)
    victim.detach_child("O")

    # Not chain B's 65 labels -- zero. One unusable residue sinks the map.
    assert scoring._assign_ss_by_pydssp(maimed) == {}

    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)
    ss_map, method = scoring.assign_dssp(maimed, "unused.pdb")
    assert method == "phi_psi", "must name the branch that actually labelled"
    assert ss_map, "phi/psi needs no O and should still cover this model"


def test_pydssp_falls_through_above_the_residue_cap(monkeypatch):
    """pydssp is O(L^2) in memory; an uncapped chain is an OOM vector.

    ANON_MAX_UPLOAD_BYTES admits ~25,900 backbone-only residues in one chain,
    which extrapolates to ~85 GB of float64 H-bond map. numpy's lazy commit
    means that allocation SUCCEEDS, so there is no MemoryError to catch -- the
    worker dies touching the pages. The cap must therefore be checked before
    the array is built, not defended with try/except.

    TWO-chain fixture with UNEQUAL chains, deliberately: with the cap at 50,
    chain A (65 residues) is over it and chain B (trimmed to 30) is under, so
    aborting the map and skipping the offending chain give different answers.
    Equal-length chains would both breach the cap and hide the difference
    again, which is how the 1HEW version of this test passed against a
    `continue` mutant of its own guard.
    """
    model = _example_model("3s7g_fc_ab.pdb")
    assert len(scoring._assign_ss_by_pydssp(model)) == 130, "fixture sanity"

    chain_b = model["B"]
    keep = [r.get_id() for r in chain_b.get_residues()][:30]
    for residue in list(chain_b.get_residues()):
        if residue.get_id() not in keep:
            chain_b.detach_child(residue.get_id())

    monkeypatch.setattr(scoring, "_PYDSSP_MAX_RESIDUES", 50)
    # Not chain B's 30 labels -- nothing at all.
    assert scoring._assign_ss_by_pydssp(model) == {}

    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)
    _, method = scoring.assign_dssp(model, "unused.pdb")
    assert method == "phi_psi"


def test_assign_dssp_falls_through_when_mkdssp_returns_no_keys(monkeypatch):
    """mkdssp can RETURN empty instead of raising, and that must fall through.

    Regression test for a real defect: the fallback chain used to live
    entirely inside ``except``, so a DSSP() call that succeeded with zero
    ``property_keys`` set method="dssp", skipped pydssp and phi/psi
    altogether, and returned ("none") -- silently scoring every patch at the
    loop floor while a perfectly readable backbone sat in the model. The
    docstring promised "skipped when it raises OR yields no labels"; only
    the raise half was implemented.
    """
    class _EmptyDSSP:
        property_keys = ()

        def __init__(self, model, pdb_path, dssp="mkdssp"):
            pass

    monkeypatch.setattr(scoring, "DSSP", _EmptyDSSP)
    sentinel = {("A", (" ", 1, " ")): "strand"}
    monkeypatch.setattr(scoring, "_assign_ss_by_pydssp", lambda model: sentinel)

    ss_map, method = scoring.assign_dssp(object(), "unused.pdb")
    assert ss_map is sentinel
    assert method == "pydssp"


def test_assign_dssp_reports_none_when_no_labels_were_produced(monkeypatch):
    """An empty map is "none", not a branch name.

    Every patch lands on the "loop" floor either way, so the SS term carried
    no signal -- crediting "phi_psi" would overstate what ran. This is the
    case with no log signal at all: _assign_ss_by_phi_psi returns {} through
    its NORMAL path when PPBuilder finds no peptides, so the "fallback also
    failed" warning never fires.
    """
    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)
    monkeypatch.setattr(scoring, "_assign_ss_by_phi_psi", lambda model: {})

    assert scoring.assign_dssp(object(), "unused.pdb") == ({}, "none")


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


@pytest.mark.filterwarnings("ignore:invalid value encountered:RuntimeWarning")
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

    assign_dssp only logs "phi_psi SS assignment failed" when
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


def test_ss_method_reaches_results_csv(tmp_path, monkeypatch):
    """The provenance value assign_dssp reports must land in the CSV.

    Before this column, "DSSP ran" and "the fallback ran" were identical in
    every artefact Scout emits, which is why the fallback went unnoticed from
    launch until 2026-08-19. This is the guard on that: it asserts the value
    travels from assign_dssp all the way to the file the user downloads.

    freesasa has no Windows wheel, so compute_rsa is swapped for Biopython's
    ShrakeRupley. That changes patch composition, not the plumbing under test.
    """
    import csv
    import shutil
    from pathlib import Path

    from Bio.PDB.SASA import ShrakeRupley

    from scout import pipeline
    from scout.sasa import STANDARD_AA

    def _rsa(structure, chain_id):
        ShrakeRupley().compute(structure[0], level="R")
        return {
            (chain_id, str(res.get_id()[1])): min(res.sasa / 200.0, 1.0)
            for res in structure[0][chain_id]
            if res.get_resname() in STANDARD_AA
        }

    monkeypatch.setattr(pipeline, "compute_rsa", _rsa)
    monkeypatch.setattr(
        pipeline, "assign_dssp", lambda model, path: ({}, "sentinel")
    )

    example = Path(__file__).resolve().parents[1] / "static" / "example" / "1HEW.pdb"
    dest = tmp_path / "input.pdb"
    shutil.copy2(example, dest)

    with (pipeline.run_pipeline(dest, "A")).open(newline="") as fh:
        rows = list(csv.DictReader(fh))

    assert rows, "pipeline produced no patches"
    assert {row["ss_method"] for row in rows} == {"sentinel"}


def test_csv_column_lists_are_in_sync():
    """scout/flags.py hand-duplicates scout/pipeline.py's column list.

    routes.py builds results_annotated.csv by feeding rows read out of
    results.csv into DictWriter(fieldnames=CSV_COLUMNS_ANNOTATED), and
    DictWriter raises ValueError on any key it has no fieldname for. So a
    column added to pipeline.CSV_COLUMNS and not to flags._CSV_COLUMNS_BASE
    breaks every analyze run in production while the whole suite stays
    green. Adding ss_method hit exactly that; this is the guard.
    """
    from scout import flags, pipeline

    assert flags._CSV_COLUMNS_BASE == pipeline.CSV_COLUMNS


# ---------------------------------------------------------------------------
# pydssp branch (added 2026-08-21)
# ---------------------------------------------------------------------------
#
# mkdssp 4.2.2 truth for static/example/1HEW.pdb chain A, generated with the
# real binary and the corrected tuple index (residue_data[2]); H/G/I folded to
# H, E/B to E, everything else to L. 129 residues, resseq 1-129 contiguous,
# every one carrying a full N/CA/C/O backbone. If the fixture ever changes,
# regenerate by running mkdssp 4.2.2 over it directly -- the script that did
# so lives in a session scratchpad, not this repo (see section 10 of
# docs/qc/scout-pydssp-adoption.md).
_1HEW_MKDSSP_TRUTH = (
    "LELLHHHHHHHHHHLLLLLELLELHHHHHHHHHHHHLLELLLEEELLLLLEEELLLLEEL"
    "LLLLELLLLLLLLLLLLLEHHHHHLLLLHHHHHHHHHHHLLLLHHHHLHHHHHHLLLLLH"
    "HHHHLLLLL"
)
_SS_LETTER = {"helix": "H", "strand": "E", "loop": "L"}


def _example_model(name="1HEW.pdb"):
    from Bio.PDB import PDBParser

    path = Path(__file__).resolve().parents[1] / "static" / "example" / name
    return PDBParser(QUIET=True).get_structure("x", str(path))[0]


def _pydssp_string(model, chain_id="A"):
    """Chain's pydssp labels as an H/E/L string, in file order."""
    ss_map = scoring._assign_ss_by_pydssp(model)
    out = []
    for residue in model[chain_id].get_residues():
        if residue.get_id()[0] != " " or residue.resname not in STANDARD_AA:
            continue
        if not all(a in residue for a in scoring._PYDSSP_BACKBONE):
            continue
        out.append(_SS_LETTER[ss_map[(chain_id, residue.get_id())]])
    return "".join(out)


def test_pydssp_reproduces_mkdssp_on_the_bundled_1hew_fixture():
    """The whole point of the branch: it must agree with the real binary.

    This is the test that fails if the vendored algorithm is subtly broken.
    The phi/psi fallback it replaced scores ~0.70 here, so the 0.90 floor is
    comfortably below the measured 0.969 and still far above anything the
    old branch could reach.
    """
    got = _pydssp_string(_example_model())
    assert len(got) == len(_1HEW_MKDSSP_TRUTH)
    agree = sum(a == b for a, b in zip(got, _1HEW_MKDSSP_TRUTH))
    assert agree / len(got) >= 0.90, (
        "pydssp agreed on %d/%d residues\n  truth  %s\n  pydssp %s"
        % (agree, len(got), _1HEW_MKDSSP_TRUTH, got)
    )
    # Not a degenerate all-one-label answer that could pass a loose ratio.
    assert set(got) == {"H", "E", "L"}


def test_pydssp_label_columns_are_not_transposed():
    """Guard the one-hot column order, which fails silently if flipped.

    scout/pydssp_numpy.assign returns np.stack([loop, helix, strand]), so
    _PYDSSP_LABELS must match that exactly. Swapping helix and strand keeps
    every label a legal value and every downstream call working -- it just
    makes the answers wrong, which is precisely the failure #161 shipped.
    """
    assert scoring._PYDSSP_LABELS == ("loop", "helix", "strand")
    # Independently: a helix-rich chain must come back helix-dominant.
    got = _pydssp_string(_example_model())
    assert got.count("H") > got.count("E"), (
        "1HEW is mostly-alpha; strand-dominant output means the columns are "
        "transposed, not that the structure changed"
    )


def test_pydssp_is_preferred_over_phi_psi(monkeypatch):
    """With no mkdssp, a full backbone must take the pydssp branch."""
    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)
    monkeypatch.setattr(
        scoring, "_assign_ss_by_phi_psi",
        lambda model: pytest.fail("phi/psi ran despite a readable backbone"),
    )

    ss_map, method = scoring.assign_dssp(_example_model(), "unused.pdb")
    assert method == "pydssp"
    assert len(ss_map) == 129


def test_phi_psi_still_covers_backbones_pydssp_cannot_read(monkeypatch):
    """An O-stripped model has no H-bonds to find, so phi/psi must catch it.

    This is why the phi/psi branch survives rather than being deleted: DSSP
    needs the carbonyl oxygen, dihedrals do not.
    """
    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)
    model = _example_model()
    for residue in list(model.get_residues()):
        if "O" in residue:
            residue.detach_child("O")

    assert scoring._assign_ss_by_pydssp(model) == {}
    ss_map, method = scoring.assign_dssp(model, "unused.pdb")
    assert method == "phi_psi"
    assert ss_map, "phi/psi produced nothing for a model that still has N/CA/C"


def test_a_chain_too_short_to_assign_aborts_the_whole_map():
    """One unassignable chain must sink the map, not just drop itself.

    turn5 reads the offset-5 diagonal, so a chain under _PYDSSP_MIN_RESIDUES
    raises on a shape mismatch inside the algorithm. Dropping only that chain
    would leave a partial map, and assign_dssp falls through only on an empty
    one -- so the run would be stamped ss_method="pydssp" while the short
    chain read as "loop" throughout.

    Uses a TWO-chain fixture deliberately: on a single-chain model "skip the
    chain" and "abort the map" are indistinguishable, and an earlier version
    of this test could not tell them apart.
    """
    model = _example_model("3s7g_fc_ab.pdb")
    assert len(scoring._assign_ss_by_pydssp(model)) == 130, "fixture sanity"

    chain = model["A"]
    keep = [r.get_id() for r in chain.get_residues()][:5]
    for residue in list(chain.get_residues()):
        if residue.get_id() not in keep:
            chain.detach_child(residue.get_id())
    assert len(list(chain.get_residues())) == 5

    # Not chain B's 65 labels -- nothing at all.
    assert scoring._assign_ss_by_pydssp(model) == {}


def _ideal_helix_coords(n=12):
    """N/CA/C/O backbone traced along an ideal alpha helix.

    Only used to prove the poisoned-import copy still computes; the accuracy
    claim is carried by the 1HEW fixture test above.
    """
    rise, turn, radius = 1.5, np.deg2rad(100.0), 2.3
    coords = []
    for i in range(n):
        base = np.array([radius * np.cos(i * turn),
                         radius * np.sin(i * turn),
                         i * rise])
        # crude but valid backbone offsets; the geometry only has to be finite
        coords.append([base + np.array(d) for d in
                       ((-0.6, 0.3, -0.4), (0.0, 0.0, 0.0),
                        (0.7, 0.5, 0.4), (0.9, 1.6, 0.6))])
    return np.asarray(coords, dtype=float)


def test_vendored_pydssp_needs_no_einops(monkeypatch):
    """The vendored copy must stay dependency-free.

    It was rewritten off einops specifically to avoid adding a dependency
    for eleven broadcast calls, and einops is NOT in requirements.txt -- so if
    an upstream re-sync quietly restores those calls, the module would blow
    up in production while a grep-for-the-word test stayed green (the word
    appears in this file's comments by design). Import it with einops
    poisoned instead, which tests the property rather than the spelling.
    """
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def _no_einops(name, *args, **kwargs):
        if name == "einops" or name.startswith("einops."):
            raise ImportError("einops is deliberately not a dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_einops)
    monkeypatch.delitem(sys.modules, "scout.pydssp_numpy", raising=False)
    vendored = importlib.import_module("scout.pydssp_numpy")

    # and it still computes, not merely imports
    onehot = vendored.assign(_ideal_helix_coords())
    assert onehot.shape == (12, 3)
    # Exactly 1 holds for THIS synthetic ideal helix. It is not a general
    # invariant of the algorithm: a real residue can satisfy the helix and
    # bridge tests at once (1 of the 678 residues in static/example, on
    # 1HEW:A), and argmax then breaks the
    # tie to helix -- DSSP's own H-over-E priority. Do not widen this to a
    # corpus without relaxing it to >= 1.
    assert onehot.sum(-1).tolist() == [1] * 12, "ideal helix: one label each"
