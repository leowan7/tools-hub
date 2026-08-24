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
    monkeypatch.setattr(scoring, "_assign_ss_by_phi_psi", lambda model, chain_id=None: sentinel)

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
    monkeypatch.setattr(scoring, "_assign_ss_by_pydssp", lambda model, chain_id=None: sentinel)

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
    # Signature must match the real assigner. With the old one-arg stub this
    # test still passed -- _try_ss swallowed the TypeError into {}, which is
    # what the stub returned anyway -- but it passed for the wrong reason and
    # the docstring's "no log signal at all" was false, since the swallowed
    # TypeError logs a warning.
    monkeypatch.setattr(
        scoring, "_assign_ss_by_phi_psi", lambda model, chain_id=None: {})

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
        pipeline, "assign_dssp", lambda model, path, chain_id=None: ({}, "sentinel")
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
    # Arity matters here beyond tidiness. pytest.fail raises Failed, which
    # subclasses BaseException, NOT Exception -- chosen deliberately so it
    # escapes _try_ss's blanket `except Exception` and actually fails the run.
    # A one-arg stub gives _try_ss a TypeError to swallow FIRST, so the body
    # never executes and the guard silently degrades.
    monkeypatch.setattr(
        scoring, "_assign_ss_by_phi_psi",
        lambda model, chain_id=None: pytest.fail(
            "phi/psi ran despite a readable backbone"),
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


def test_assign_dssp_labels_only_the_scored_chain():
    """run_pipeline reads one chain's labels, so only one chain is computed.

    Every patch comes from model[chain_id] and both _majority_ss and
    _continuous_ss_score look up (chain_id, ...), so labelling the rest of
    the model was pure waste -- and since pydssp is O(L^2) per chain with no
    cap on chain COUNT, it was how a multi-chain upload multiplied the
    per-chain ceiling. Measured on a 13-chain model: 13.4x faster scoped, and
    the scoped cost is FLAT in chain count.
    """
    model = _example_model("3s7g_fc_ab.pdb")

    both = scoring._assign_ss_by_pydssp(model)
    assert {c for c, _ in both} == {"A", "B"}, "fixture sanity: two chains"
    assert len(both) == 130

    only_a = scoring._assign_ss_by_pydssp(model, "A")
    assert {c for c, _ in only_a} == {"A"}, "chain B must not be labelled"
    assert len(only_a) == 65
    # Same answers for the chain that IS scored -- this is a scoping change,
    # not a scoring change. Chains are fed to pydssp independently either way.
    assert only_a == {k: v for k, v in both.items() if k[0] == "A"}


def test_an_unreadable_neighbour_no_longer_sinks_the_scored_chain(monkeypatch):
    """The all-or-nothing rule cost a whole model when one chain was broken.

    That trade was real while assignment was whole-model: a CA-only chain B
    sent chain A to phi/psi (~70% agreement) even though pydssp could read A
    perfectly (~97.9%). Scoping retires it -- B is never looked at, so it can
    neither sink the map nor cost an allocation. The all-or-nothing rule
    still holds, now over the chain actually in scope.
    """
    import copy

    model = _example_model("3s7g_fc_ab.pdb")
    for residue in list(model["B"].get_residues()):
        for atom_id in [a.get_id() for a in residue if a.get_id() != "CA"]:
            residue.detach_child(atom_id)

    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)

    # Whole-model: chain B's missing backbone aborts pydssp for everyone.
    _, method_all = scoring.assign_dssp(copy.deepcopy(model), "unused.pdb")
    assert method_all == "phi_psi", "the old behaviour, kept reachable"

    # Scoped: chain A is read by pydssp regardless of what B looks like.
    ss_map, method_a = scoring.assign_dssp(copy.deepcopy(model), "unused.pdb", "A")
    assert method_a == "pydssp"
    assert len(ss_map) == 65
    assert {c for c, _ in ss_map} == {"A"}


def test_the_mkdssp_branch_falls_through_when_it_misses_the_scored_chain(monkeypatch):
    """A map covering only OTHER chains must not suppress the fall-through.

    assign_dssp advances on "did this branch produce labels?". Unfiltered,
    mkdssp returning keys for chain B alone made that test true while the
    scored chain A had none -- so the run was stamped ss_method="dssp" with
    every chain-A patch on the "loop" floor. Restricting each branch to the
    scored chain turns that question into "did it label the chain we read?".
    """
    class _OtherChainOnlyDSSP:
        property_keys = (("B", (" ", 1, " ")),)

        def __init__(self, model, pdb_path, dssp="mkdssp"):
            pass

        def __getitem__(self, key):
            return (0, "A", "H", 0.0, 0.0, 0.0)

    monkeypatch.setattr(scoring, "DSSP", _OtherChainOnlyDSSP)
    sentinel = {("A", (" ", 1, " ")): "strand"}
    monkeypatch.setattr(scoring, "_assign_ss_by_pydssp",
                        lambda model, chain_id=None: sentinel)

    ss_map, method = scoring.assign_dssp(object(), "unused.pdb", "A")
    assert method == "pydssp", "chain B's labels must not stand in for chain A's"
    assert ss_map is sentinel


def test_phi_psi_is_scoped_to_the_chain_too(monkeypatch):
    """The third filter, and it fails the same way the other two would.

    phi/psi is the branch that actually runs when pydssp cannot read the
    backbone, so an unscoped one is the live hole: score a CA-only chain A
    beside an intact chain B and phi/psi labels B, which makes the map
    non-empty, which stops the fall-through -- so the run reports
    ss_method="phi_psi" while every chain-A patch sits on the "loop" floor.
    Scoped, neither branch can read A and the honest answer is "none".
    """
    model = _example_model("3s7g_fc_ab.pdb")
    for residue in list(model["A"].get_residues()):
        for atom_id in [a.get_id() for a in residue if a.get_id() != "CA"]:
            residue.detach_child(atom_id)

    def _boom(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _boom)

    # Chain B is intact and would happily supply 65 labels -- none of which
    # describe the chain being scored.
    ss_map, method = scoring.assign_dssp(model, "unused.pdb", "A")
    assert ss_map == {}, "chain B's labels must not stand in for chain A's"
    assert method == "none"


def test_run_pipeline_passes_the_scored_chain_to_assign_dssp(tmp_path, monkeypatch):
    """The call site is what makes this a bound, not just an option.

    assign_dssp defaults chain_id to None (whole model) so the tests above
    can drive multi-chain behaviour directly. That default means a caller
    which forgets the argument silently restores the unbounded O(L^2)-times-
    chain-count cost, and every other test here would stay green. This is the
    guard on the production call actually passing it.
    """
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

    seen = []

    def _record(model, path, chain_id=None):
        seen.append(chain_id)
        return {}, "recorded"

    monkeypatch.setattr(pipeline, "compute_rsa", _rsa)
    monkeypatch.setattr(pipeline, "assign_dssp", _record)

    example = Path(__file__).resolve().parents[1] / "static" / "example" / "1HEW.pdb"
    dest = tmp_path / "input.pdb"
    shutil.copy2(example, dest)
    pipeline.run_pipeline(dest, "A")

    assert seen == ["A"], f"run_pipeline must scope SS to the chain it scores, got {seen}"


# ---------------------------------------------------------------------------
# Selenomethionine: a modified residue that must NOT break the backbone.
#
# MSE is recorded as HETATM, so every standard-residue test in this package
# rejected it on the hetflag alone. For pydssp that is not a missing label,
# it is a CLOSED GAP: the coordinate array welds the residues either side of
# the MSE together, corrupting the next residue's pseudo-H and sliding the
# fixed i->i+3/4/5 turn and bridge offsets onto renumbered indices.
#
# It was also invisible. MSE appeared in neither the standard-residue count
# nor the labelled set, so the all-or-nothing guard saw no mismatch and the
# run still stamped ss_method="pydssp".
# ---------------------------------------------------------------------------

_EXAMPLE_PDB = Path(__file__).resolve().parents[1] / "static" / "example" / "1HEW.pdb"


def _as_selenomethionine(text: str, record: str = "HETATM") -> str:
    """Rewrite every MET record as MSE, the way a SeMet deposition reads.

    ``record`` picks the spelling: real depositions use HETATM, while design
    and refinement pipelines often re-emit the same residue as ATOM.
    """
    out = []
    for line in text.splitlines(True):
        if line.startswith(("ATOM  ", "HETATM")) and line[17:20] == "MET":
            line = record.ljust(6) + line[6:17] + "MSE" + line[20:]
        out.append(line)
    return "".join(out)


def _model_from_text(text: str):
    import io

    from Bio.PDB import PDBParser

    return PDBParser(QUIET=True).get_structure("x", io.StringIO(text))[0]


def _labels_ignoring_hetflag(ss_map: dict) -> dict:
    """Key labels by (chain, resseq, icode) so MSE matches its MET twin."""
    return {(cid, rid[1], rid[2]): label for (cid, rid), label in ss_map.items()}


@pytest.mark.parametrize("record", ["HETATM", "ATOM"])
def test_selenomethionine_does_not_break_the_backbone(record):
    """MET -> MSE is a chemistry-preserving edit, so labels must not move.

    1HEW carries MET at 12 and 105, both interior, so this fixture really
    does exercise two junctions rather than trivially passing.

    Before the fix this failed twice over: the two MSE residues were absent
    from the map entirely, AND residues near them changed label because the
    array had closed over the gap.
    """
    text = _EXAMPLE_PDB.read_text(encoding="utf-8", errors="replace")
    plain = _model_from_text(text)
    semet = _model_from_text(_as_selenomethionine(text, record))

    n_mse = sum(
        1 for r in semet["A"].get_residues() if r.resname.strip() == "MSE"
    )
    assert n_mse == 2, f"fixture must contain MSE to be a test at all, got {n_mse}"

    want = _labels_ignoring_hetflag(scoring._assign_ss_by_pydssp(plain, "A"))
    got = _labels_ignoring_hetflag(scoring._assign_ss_by_pydssp(semet, "A"))

    assert want, "the control arm produced no labels; the fixture is broken"
    missing = sorted(want.keys() - got.keys())
    assert not missing, f"MSE residues were dropped from the map: {missing}"

    moved = {k: (want[k], got[k]) for k in want if want[k] != got[k]}
    assert not moved, (
        "MET->MSE changed %d labels, so the coordinate array closed over the "
        "modified residue: %s" % (len(moved), sorted(moved.items())[:8])
    )


def _rewrite_residues(text, targets, resname, record):
    """Re-record the named chain-A residues under a different name/record."""
    out = []
    for line in text.splitlines(True):
        if line.startswith(("ATOM  ", "HETATM")) and line[21] == "A":
            if int(line[22:26]) in targets:
                line = record.ljust(6) + line[6:17] + resname + line[20:]
        out.append(line)
    return "".join(out)


@pytest.mark.parametrize(
    ("label", "targets"),
    [
        ("first residue", {1}),
        ("last residue", {129}),
        ("two consecutive", {50, 51}),
        ("both termini and middle", {1, 65, 129}),
    ],
)
def test_selenomethionine_at_chain_edges_is_handled_like_any_residue(label, targets):
    """MSE at a terminus or in a run must behave exactly like the same atoms
    recorded as MET.

    The control is the SAME COORDINATES written as `ATOM ... MET`, so pydssp --
    which reads coordinates and nothing else -- must return identical labels.
    Any difference means residue selection and array construction disagree at
    the edges.

    Kept because the current selector is a plain filter where an edge bug is
    unlikely, but a future continuity or peptide-bond check (see the residual
    risk noted at _PYDSSP_MODIFIED_AA) would land exactly here.
    """
    text = _EXAMPLE_PDB.read_text(encoding="utf-8", errors="replace")
    variant = _model_from_text(_rewrite_residues(text, targets, "MSE", "HETATM"))
    control = _model_from_text(_rewrite_residues(text, targets, "MET", "ATOM  "))

    got = _labels_ignoring_hetflag(scoring._assign_ss_by_pydssp(variant, "A"))
    want = _labels_ignoring_hetflag(scoring._assign_ss_by_pydssp(control, "A"))

    assert want, "control arm produced no labels; fixture is broken"
    assert want.keys() == got.keys(), (
        "%s: MSE changed which residues are labelled" % label
    )
    assert want == got, "%s: MSE changed %d labels" % (
        label,
        sum(1 for k in want if want[k] != got[k]),
    )


def test_selenomethionine_reaches_the_pydssp_branch_not_a_fallback(monkeypatch):
    """The labels above must come from pydssp, not from phi/psi rescuing it.

    Without this, the equality test would still pass if MSE made the pydssp
    map empty and assign_dssp quietly fell through -- the labels would agree
    with each other while ss_method silently changed.
    """
    text = _EXAMPLE_PDB.read_text(encoding="utf-8", errors="replace")
    semet = _model_from_text(_as_selenomethionine(text))

    def _no_binary(model, pdb_path, dssp="mkdssp"):
        raise FileNotFoundError("mkdssp")

    monkeypatch.setattr(scoring, "DSSP", _no_binary)
    # Three-arg stub, matching _try_ss's call signature: a one-arg stub would
    # hand _try_ss a TypeError to swallow before the body ran, and the branch
    # would look like it fell through for the wrong reason.
    monkeypatch.setattr(
        scoring,
        "_assign_ss_by_phi_psi",
        lambda model, chain_id=None: pytest.fail(
            "SeMet structure fell through to the phi/psi fallback"
        ),
    )

    ss_map, method = scoring.assign_dssp(semet, str(_EXAMPLE_PDB), "A")

    assert method == "pydssp", f"SeMet structure fell through to {method!r}"
    assert ss_map


def test_modified_residues_are_counted_by_the_all_or_nothing_guard():
    """An MSE missing a backbone atom must sink the map, like any residue.

    Admitting MSE to the coordinate array also admits it to the completeness
    guard. If it were admitted to one but not the other, a broken MSE would
    be silently skipped and the map would still report itself complete --
    exactly the partial-map failure the guard exists to stop.
    """
    text = _EXAMPLE_PDB.read_text(encoding="utf-8", errors="replace")
    semet = _as_selenomethionine(text)
    stripped = "".join(
        line for line in semet.splitlines(True)
        if not (line[17:20] == "MSE" and line[12:16].strip() == "O")
    )
    assert stripped != semet, "fixture edit removed nothing"

    ss_map = scoring._assign_ss_by_pydssp(_model_from_text(stripped), "A")
    assert ss_map == {}, (
        "an MSE with no carbonyl O must abort the whole map rather than be "
        "quietly skipped"
    )


def test_the_modified_residue_whitelist_does_not_admit_ligands_or_water():
    """Only MSE/SEC get in. A het group with backbone atom names must not.

    The rejected alternative was "accept anything carrying N/CA/C/O", which
    would thread a genuine ligand into the polymer.
    """
    def _residue(hetflag, resname):
        res = Residue((hetflag, 1, " "), resname, "")
        for name in scoring._PYDSSP_BACKBONE:
            res.add(Atom(name, np.zeros(3), 0.0, 1.0, " ", name, 1, "C"))
        return res

    assert scoring._is_pydssp_polymer_residue(_residue(" ", "ALA"))
    assert scoring._is_pydssp_polymer_residue(_residue("H_MSE", "MSE"))
    assert scoring._is_pydssp_polymer_residue(_residue("H_SEC", "SEC"))
    # An ATOM-spelled MSE is the same chemistry and must also be accepted.
    assert scoring._is_pydssp_polymer_residue(_residue(" ", "MSE"))

    assert not scoring._is_pydssp_polymer_residue(_residue("W", "HOH"))
    assert not scoring._is_pydssp_polymer_residue(_residue("H_NAG", "NAG"))
    assert not scoring._is_pydssp_polymer_residue(_residue("H_LIG", "LIG"))
    # A HETATM amino acid that is NOT whitelisted stays out: it may be
    # chemically odd enough that welding it into the chain is wrong.
    assert not scoring._is_pydssp_polymer_residue(_residue("H_SEP", "SEP"))

    # A HETATM carrying a CANONICAL amino-acid name also stays out: a free
    # alanine in the solvent is a ligand, not a link in the chain. This is the
    # case that pins the hetflag gate on the canonical branch. Mutation testing
    # found that without these two lines the gate can be deleted outright --
    # `return resname in STANDARD_AA` -- and all 29 tests still pass, because
    # every other negative case here uses a non-canonical resname and is
    # rejected by the resname check alone.
    assert not scoring._is_pydssp_polymer_residue(_residue("H_ALA", "ALA"))
    assert not scoring._is_pydssp_polymer_residue(_residue("H_GLY", "GLY"))


def _chain_b_of_three(resname: str, record: str) -> str:
    """1HEW chain A, plus a 3-residue chain B of the given residue type."""
    text = _EXAMPLE_PDB.read_text(encoding="utf-8", errors="replace")
    keep = [
        line for line in text.splitlines(True)
        if line.startswith("ATOM") and line[21] == "A"
    ]
    out, serial = [], 1
    for i in range(3):
        for name, element in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")):
            x = 40.0 + i * 3.5 + (0.0 if name == "N" else 1.2 if name == "CA" else 2.4)
            out.append(
                f"{record:<6}{serial:5d}  {name:<3} {resname} B{i + 1:4d}    "
                f"{x:8.3f}{20.0:8.3f}{20.0:8.3f}  1.00 20.00          {element:>2}\n"
            )
            serial += 1
    return "".join(keep) + "".join(out) + "END\n"


def test_a_short_mse_only_chain_behaves_like_a_short_ordinary_chain():
    """Admitting MSE means an MSE-only chain COUNTS. That must be consistent.

    A 3-residue chain trips _PYDSSP_MIN_RESIDUES, and the all-or-nothing rule
    then empties the whole map. That is what a 3-residue ALA-only chain has
    always done. Before MSE was admitted, a 3-residue MSE-only chain was
    instead INVISIBLE -- it had no standard residues, so it was skipped and the
    map survived. The asymmetry was the bug; this test pins the symmetry so
    nobody "fixes" it back by special-casing MSE out of the residue count.

    Only reachable via chain_id=None. run_pipeline always passes a chain, and
    the scoped call is asserted below to be unaffected.
    """
    ala = _model_from_text(_chain_b_of_three("ALA", "ATOM"))
    mse = _model_from_text(_chain_b_of_three("MSE", "HETATM"))

    assert scoring._assign_ss_by_pydssp(ala, None) == {}, (
        "a 3-residue canonical chain should sink the whole-model map"
    )
    assert scoring._assign_ss_by_pydssp(mse, None) == {}, (
        "a 3-residue MSE-only chain must sink it the same way, not be skipped"
    )

    # Scoped to the chain actually being scored, the short neighbour is never
    # looked at -- which is why production is unaffected either way.
    assert len(scoring._assign_ss_by_pydssp(ala, "A")) == 129
    assert len(scoring._assign_ss_by_pydssp(mse, "A")) == 129


def test_modified_residues_count_toward_the_max_residue_cap(monkeypatch):
    """MSE counts toward _PYDSSP_MAX_RESIDUES, so the change is NOT monotone.

    Admitting MSE/SEC inflates ``len(standard)``, which is what the O(L^2)
    cap is checked against. A chain whose canonical count falls in
    ``(cap - n_MSE, cap]`` therefore crosses the cap once MSE is admitted and
    loses its ENTIRE map -- a real downgrade, on the SCOPED path that
    production uses.

    That is deliberate, not a bug to fix here: the cap bounds the allocation
    pydssp actually makes, and ``len(standard)`` is now the honest size where
    before it under-counted. What such a chain loses is a map that was
    junction-corrupted at every MSE anyway, so the trade is "corrupted pydssp"
    for "phi/psi", with ss_method still telling the truth.

    Pinned so that the non-monotone band is an executable fact rather than a
    comment, and so that anyone re-deriving the cap against a different count
    has to do it on purpose. The 178-chain regression sweep could not see this
    -- no chain in it came near 2000 residues.
    """
    path = Path(__file__).resolve().parents[1] / "static" / "example" / "1HEW.pdb"
    text = path.read_text()

    semet = _model_from_text(_as_selenomethionine(text))
    chain = semet["A"]

    # 1HEW chain A is 129 residues, two of them MET -> MSE here. The old
    # selector counted only the 127 canonical ones; this one counts all 129.
    accepted = [r for r in chain.get_residues() if scoring._is_pydssp_polymer_residue(r)]
    canonical_only = [
        r
        for r in chain.get_residues()
        if r.get_id()[0] == " " and r.resname.strip() in STANDARD_AA
    ]
    assert len(accepted) == 129
    assert len(canonical_only) == 127

    # Pinned at the count the OLD selector would have reported, the chain no
    # longer fits -- the two MSE are what push it over, and the whole map goes.
    monkeypatch.setattr(scoring, "_PYDSSP_MAX_RESIDUES", 127)
    assert scoring._assign_ss_by_pydssp(semet, "A") == {}

    # Room for the MSE too, and it comes straight back in full.
    monkeypatch.setattr(scoring, "_PYDSSP_MAX_RESIDUES", 129)
    assert len(scoring._assign_ss_by_pydssp(semet, "A")) == 129
