"""Chain-list guards for Epitope Scout's structure parser.

scout/parser.py had no dedicated test module. MSE-bearing structures were not
absent from the suite -- tests/test_scout_ss_assignment.py is full of them --
but they exercise scout/scoring.py, so nothing pinned this parser's behaviour
on one.

That is how STANDARD_AA came to contain MSE and SEC while the selector beside
it also demanded a blank hetflag. The resname test did match; only the
conjunction failed, and only for the HETATM spelling the PDB uses for MSE. So
a structure carrying HETATM-spelled MSE or SEC came out of the picker short
by the number of such residues -- except where one is an altloc partner of an
ATOM-spelled residue at the same number, or collides with a polymer position,
both of which counted correctly already.

Same root cause as the two fixes already shipped in scout/scoring.py: PR #187
(pydssp backbone, main ec2c401) and PR #190 (phi/psi peptide, main 413bf5d).
Evidence: docs/qc/scout-pydssp-adoption.md
"""

from pathlib import Path

from Bio.PDB.Residue import Residue

from scout import parser
from scout.parser import parse_pdb

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


def _counts(tmp_path, text, name="s.pdb"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    result = parse_pdb(path)
    assert result.error == "", result.error
    return {chain.id: chain.residue_count for chain in result.chains}


def test_selenomethionine_does_not_shorten_the_chain(tmp_path):
    """MET -> MSE is a chemistry-preserving edit, so the length must not move.

    This is the reported bug. On PDB 1B24 chain A -- 173 polymer residues, 3 of
    them MSE and no MET at all -- the picker read "170 res" on main at
    413bf5d, and scout/parser.py is byte-identical from there to this
    commit's parent, so the reading is the pre-fix one. Reproduced here on the bundled fixture, whose 2 MET make the same
    edit cost 2 residues.

    Both spellings are checked. HETATM is what the PDB deposits; ATOM is what
    a refinement pipeline re-emits, and it passed even before the fix, so
    asserting only that one would not have caught anything.
    """
    text = _EXAMPLE_PDB.read_text(encoding="utf-8", errors="replace")
    n_met = sum(
        1 for line in text.splitlines()
        if line.startswith("ATOM  ")
        and line[17:20] == "MET"
        and line[12:16].strip() == "CA"
    )
    assert n_met == 2, f"fixture must contain MET to be a test at all, got {n_met}"

    native = _counts(tmp_path, text, "native.pdb")
    assert native == {"A": 129}

    for record in ("HETATM", "ATOM  "):
        semet = _counts(tmp_path, _as_selenomethionine(text, record), "semet.pdb")
        assert semet == native, (
            f"MET->MSE spelled {record.strip()} changed the chain length "
            f"{native} -> {semet}; the {n_met} MSE were dropped"
        )


def test_a_chain_of_only_modified_residues_reaches_the_picker(tmp_path):
    """A wholly-MSE chain must be offered, not silently dropped.

    ``if protein_residues:`` skips any chain with nothing in it, so before the
    fix a chain built only from modified residues was not merely miscounted --
    it never appeared in the dropdown. (POSTing the chain id directly still
    reached run_pipeline, which by inspection refuses it with a 422 -- the
    picker is what omitted it, not the route. This test asserts the picker
    contents only; nothing here exercises that 422.)
    """
    text = _EXAMPLE_PDB.read_text(encoding="utf-8", errors="replace")
    keep = [
        line for line in text.splitlines(True)
        if line.startswith("ATOM") and line[21] == "A"
    ]
    extra, serial = [], 90000
    for i in range(3):
        for name, element in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")):
            x = 40.0 + i * 3.5 + (0.0 if name == "N" else 1.2 if name == "CA" else 2.4)
            extra.append(
                f"{'HETATM':<6}{serial:5d}  {name:<3} MSE B{i + 1:4d}    "
                f"{x:8.3f}{20.0:8.3f}{20.0:8.3f}  1.00 20.00          {element:>2}\n"
            )
            serial += 1

    chains = _counts(tmp_path, "".join(keep) + "".join(extra) + "END\n")
    assert chains == {"A": 129, "B": 3}, (
        f"the MSE-only chain B is missing from the picker: {chains}"
    )


def test_partial_selenomethionine_counts_its_position_once(tmp_path):
    """A MET/MSE altloc pair at one residue number is ONE residue.

    Incomplete Se incorporation is deposited as two altlocs sharing a residue
    number -- ATOM ... AMET and HETATM ... BMSE. Their hetflags differ, so
    Biopython does NOT merge them into a DisorderedResidue; it yields two
    separate Residue objects with ids (" ", 2, " ") and ("H_MSE", 2, " ").

    The pre-fix selector took only the MET and was right by accident. Admitting
    MSE takes both, which would count the position twice and make the displayed
    length worse than the bug this module exists to fix -- on this fixture, 4
    for a 3-residue chain. Pinned because it is the one input class where the
    fix could regress the number it is meant to correct.
    """
    lines, serial = [], 1

    def emit(record, altloc, resname, resnum, occupancy):
        nonlocal serial
        for name, element in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")):
            lines.append(
                f"{record:<6}{serial:5d}  {name:<3}{altloc}{resname} A{resnum:4d}    "
                f"{10.0 + resnum * 3.5:8.3f}{20.0:8.3f}{20.0:8.3f}"
                f"  {occupancy:4.2f} 20.00          {element:>2}\n"
            )
            serial += 1

    emit("ATOM  ", " ", "ALA", 1, 1.00)
    emit("ATOM  ", "A", "MET", 2, 0.50)
    emit("HETATM", "B", "MSE", 2, 0.50)
    emit("ATOM  ", " ", "GLY", 3, 1.00)

    assert _counts(tmp_path, "".join(lines) + "END\n") == {"A": 3}


def test_insertion_codes_are_distinct_positions(tmp_path):
    '''100, 100A and 100B are three residues, not one.

    The dedupe above keys on (resseq, icode). Keying on resseq alone still
    passes every other test in this file -- mutation testing confirmed it --
    while silently collapsing an insertion-coded run (100, 100A, 100B) inside
    an antibody CDR to a single residue.
    '''
    lines, serial = [], 1
    for icode in (" ", "A", "B"):
        for name, element in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")):
            lines.append(
                f"ATOM  {serial:5d}  {name:<3} ALA A 100{icode}   "
                f"{10.0:8.3f}{20.0:8.3f}{20.0:8.3f}  1.00 20.00          {element:>2}\n"
            )
            serial += 1

    assert _counts(tmp_path, "".join(lines) + "END\n") == {"A": 3}


def test_the_modified_residue_whitelist_does_not_admit_ligands_or_water():
    """Only MSE/SEC skip the hetflag gate. Nothing else may.

    The rejected alternative was to drop the hetflag test outright, the shape
    scout/scoring.py::_ScoutPPBuilder uses. That is safe there only because
    _is_connected then rejects a free residue on peptide-bond geometry. There
    is no such test here, so the gate is the only thing separating a chain
    link from a ligand.
    """
    def _residue(hetflag, resname):
        return Residue((hetflag, 1, " "), resname, "")

    assert parser._is_polymer_residue(_residue(" ", "ALA"))
    assert parser._is_polymer_residue(_residue("H_MSE", "MSE"))
    assert parser._is_polymer_residue(_residue("H_SEC", "SEC"))
    # An ATOM-spelled MSE is the same chemistry and must also be accepted.
    assert parser._is_polymer_residue(_residue(" ", "MSE"))

    assert not parser._is_polymer_residue(_residue("W", "HOH"))
    assert not parser._is_polymer_residue(_residue("H_NAG", "NAG"))
    # A HETATM amino acid that is NOT whitelisted stays out.
    assert not parser._is_polymer_residue(_residue("H_SEP", "SEP"))

    # A HETATM carrying a CANONICAL amino-acid name also stays out: a free
    # alanine in the solvent is a ligand, not a link in the chain. These two
    # are what pin the hetflag gate on the canonical branch -- without them
    # the gate can be deleted outright (`return resname in STANDARD_AA`) and
    # every other case here still passes on the resname check alone. The same
    # hole was found by mutation testing on scoring.py's copy.
    assert not parser._is_polymer_residue(_residue("H_ALA", "ALA"))
    assert not parser._is_polymer_residue(_residue("H_GLY", "GLY"))


def test_standard_aa_is_still_the_documented_22_names():
    """Splitting _MODIFIED_AA out must not change the public export.

    STANDARD_AA's docstring promises 22 names and it is now built as a union,
    which is exactly the kind of edit that quietly drops a member. Nothing
    outside the module imports it, so this test is what pins it.
    """
    assert parser.STANDARD_AA == frozenset({
        "ALA", "ARG", "ASN", "ASP", "CYS",
        "GLN", "GLU", "GLY", "HIS", "ILE",
        "LEU", "LYS", "MET", "PHE", "PRO",
        "SER", "THR", "TRP", "TYR", "VAL",
        "MSE", "SEC",
    })
    assert parser._MODIFIED_AA < parser.STANDARD_AA
