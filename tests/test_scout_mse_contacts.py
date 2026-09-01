"""Contact-detection guards for selenomethionine and selenocysteine.

MSE and SEC are ordinary polymer residues that the PDB deposits as HETATM.
Four residue filters gated on the hetflag alone (`residue.id[0] != " "`), so a
selenomethionine sitting in an epitope was invisible to contact detection: it
contributed no atoms, and a HETATM-spelled one could not appear in
``contact_residues``. (An ATOM-spelled MSE or SEC always could -- Biopython
gives those a blank hetflag. 1CC1's SEC 492 is deposited that way.)

There are two such lists and they differ: ``_compute_contacts``' is user-facing
only, while ``detect_interfaces``' is both rendered and unioned into the scored
contact set. For the second, the residue was absent from a scored input.

Fourth site of the same root cause, after #187, #190 and #195. The full gate
inventory is in the commit message, including two gates in scout/pipeline.py
that are a deliberate design choice -- patch construction, NOT the freesasa
radii reason, which belongs to scout/sasa.py -- and are not neutral.

Each test re-spells a residue that is DEMONSTRABLY a contact in the unmodified
fixture, so a regression cannot hide behind a residue that was never reported
in the first place. The fixtures are checked in and parsed offline; nothing
here reaches the network.
"""

import pathlib
import tempfile

import pytest

from scout import epitope_db, interfaces

pytest.importorskip("Bio")
pytest.importorskip("numpy")

_EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "static" / "example"
_FC_AB = _EXAMPLES / "3s7g_fc_ab.pdb"
_FC_DIMER = _EXAMPLES / "3ave_igg1_fc_dimer.pdb"


def _respell_as(text: str, chain: str, resnum: int, resname: str = "MSE") -> str:
    """Rewrite one residue's ATOM records as HETATM/MSE, leaving geometry alone.

    Not the MET->MSE control the earlier fixes used -- this re-spells whatever
    residue the fixture reports as a contact, a GLY and a GLN in practice. It
    does not need to preserve chemistry: admission reads the hetflag, the
    resname and backbone geometry, none of which depend on the original amino
    acid. The atoms do not move, so any change is attributable to the record
    type and residue name alone.
    """
    out = []
    for line in text.splitlines():
        if (
            line.startswith("ATOM")
            and len(line) > 26
            and line[21] == chain
            and line[22:26].strip() == str(resnum)
        ):
            line = "HETATM" + line[6:17] + resname + line[20:]
        out.append(line)
    return "\n".join(out) + "\n"


def test_selenomethionine_in_the_antigen_is_still_a_contact():
    """An MSE in the antigen must keep its place in contact_residues.

    _compute_contacts gated on the hetflag, so re-spelling a contact residue as
    MSE removed it from the returned list entirely — a real epitope residue
    silently missing from user-facing output.
    """
    text = _FC_AB.read_text(encoding="utf-8", errors="replace")

    native = epitope_db._compute_contacts(text, "A", ["B"])
    assert native, "fixture must yield contacts for this to be a test at all"

    target = native[0]
    semet = _respell_as(text, "A", target)
    after = epitope_db._compute_contacts(semet, "A", ["B"])

    assert target in after, (
        f"residue {target} is a contact in the native structure but vanished "
        f"once spelled MSE/HETATM: {native} -> {after}"
    )
    assert sorted(after) == sorted(native), (
        f"re-spelling one residue must not disturb the rest: {native} -> {after}"
    )


def test_selenomethionine_in_the_antibody_still_contributes_atoms():
    """The antibody-side gate matters too: its atoms define the contact sphere.

    Dropping an MSE from the antibody does not remove an antigen residue by
    name; it removes the atoms that would have brought antigen residues within
    the cutoff, so the antigen contact list can only shrink.
    """
    text = _FC_AB.read_text(encoding="utf-8", errors="replace")
    native = epitope_db._compute_contacts(text, "A", ["B"])

    # Every OTHER residue of the partner chain. Re-spelling ALL of them would
    # leave the chain with no canonical residue to anchor on, and admission
    # deliberately refuses to seed a run of modified residues from itself --
    # that is what stops a free SeMet peptide minting an interface.
    partner_resnums = sorted(
        {
            int(line[22:26])
            for line in text.splitlines()
            if line.startswith("ATOM") and len(line) > 26 and line[21] == "B"
        }
    )
    semet = text
    for rn in partner_resnums[::2]:
        semet = _respell_as(semet, "B", rn)

    after = epitope_db._compute_contacts(semet, "A", ["B"])
    assert sorted(after) == sorted(native), (
        f"antibody chain spelled entirely MSE changed the antigen contacts: "
        f"{native} -> {after}"
    )


def test_selenomethionine_at_a_detected_interface_survives(tmp_path):
    """Same defect in scout/interfaces.py, which feeds the scoring path."""
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")
    native = interfaces.detect_interfaces(str(_FC_DIMER), "A")
    assert native, "fixture must yield an interface for this to be a test"

    native_contacts = sorted(native[0]["contact_residues"])
    target = native_contacts[0]

    # tmp_path, NOT the fixture directory. Writing a probe into the checked-in
    # static/example/ is bad hygiene on its own; note that no test would have
    # caught it, because the deploy-path tests enumerate `git ls-files`, which
    # never sees an untracked file.
    semet_path = tmp_path / "mse_probe.pdb"
    semet_path.write_text(_respell_as(text, "A", target), encoding="utf-8")
    after = interfaces.detect_interfaces(str(semet_path), "A")
    assert after, "the interface disappeared entirely once a residue was MSE"
    after_contacts = sorted(after[0]["contact_residues"])

    assert target in after_contacts, (
        f"residue {target} is an interface contact natively but vanished once "
        f"spelled MSE/HETATM: {native_contacts} -> {after_contacts}"
    )
    assert after_contacts == native_contacts, (
        f"re-spelling one residue must not disturb the rest: "
        f"{native_contacts} -> {after_contacts}"
    )


def test_the_gates_still_reject_water_and_ordinary_ligands():
    """The fix admits exactly two names, not every HETATM.

    Without this, "stop dropping HETATM" could be implemented by deleting the
    gate, which would let waters and sugars be reported as epitope contacts.
    """
    # No constant assertion here: emptying the set or dropping SEC now fails
    # behavioural tests in this module, so asserting the literal would only
    # restate what those already prove.
    text = _FC_AB.read_text(encoding="utf-8", errors="replace")
    native = epitope_db._compute_contacts(text, "A", ["B"])
    target = native[0]

    # Spelling a contact as a sugar or a water must drop it, exactly as before.
    for resname in ("NAG", "HOH"):
        out = []
        for line in text.splitlines():
            if (
                line.startswith("ATOM")
                and len(line) > 26
                and line[21] == "A"
                and line[22:26].strip() == str(target)
            ):
                line = "HETATM" + line[6:17] + resname + line[20:]
            out.append(line)
        after = epitope_db._compute_contacts("\n".join(out) + "\n", "A", ["B"])
        assert target not in after, (
            f"residue {target} spelled {resname} was still reported as a "
            f"contact; the gate has been opened too wide"
        )


def test_sequence_identity_denominator_is_the_shorter_sequence():
    """Pins the behaviour whose docstring used to claim the opposite.

    The docstring said identity was divided by the length of the LONGER
    sequence; the code uses min(). That mattered: it is the reason the MSE gate
    in _extract_chain_sequence cannot flip the 0.70 validation threshold, and
    the wrong docstring would have made that look like a live bug.
    """
    full = "ACDEFGHIKLMNPQRSTVWY"
    truncated = full[:10]

    identity = epitope_db._sequence_identity(full, truncated)
    assert identity == pytest.approx(1.0), (
        "a perfect prefix must score 1.0 against min-length; it would score "
        "0.5 if the denominator were the longer sequence"
    )
    assert identity >= epitope_db._MIN_VALIDATION_IDENTITY


# ---------------------------------------------------------------------------
# Guards added after independent review found three holes in the tests above:
# the partner-chain gate had no behavioural coverage at all, SEC was pinned
# only by a constant assertion, and nothing exercised the two defects that
# admitting MSE introduces (altloc double-count, free-ligand phantom contacts).
# ---------------------------------------------------------------------------

def _pdb_atom(serial, name, resname, chain, resi, x, y, z, het=False):
    """One PDB ATOM/HETATM line at exact column positions."""
    line = list(" " * 80)

    def put(text, start):  # 1-indexed PDB columns
        for offset, char in enumerate(str(text)):
            line[start - 1 + offset] = char

    put("HETATM" if het else "ATOM  ", 1)
    put(f"{serial:5d}", 7)
    put(f" {name:<3s}", 13)
    put(f"{resname:>3s}", 18)
    put(chain, 22)
    put(f"{resi:4d}", 23)
    put(f"{x:8.3f}", 31)
    put(f"{y:8.3f}", 39)
    put(f"{z:8.3f}", 47)
    put("  1.00 20.00", 55)
    put("C", 77)
    return "".join(line).rstrip()


def _chain_with_free_ligand(ligand_resname):
    """Target chain T ringing a pocket, plus ONE free ligand in chain L."""
    lines, serial = [], 1
    for k in range(8):
        for atom_name in ("N", "CA", "C", "O"):
            lines.append(
                _pdb_atom(serial, atom_name, "ALA", "T", k + 1,
                          3.0 + 0.2 * k, 0.3 * k, 0.4 * k)
            )
            serial += 1
    for k in range(8):
        lines.append(
            _pdb_atom(serial, f"C{k}", ligand_resname, "L", 500,
                      0.5 + 0.2 * k, 0.2 * k, 0.1 * k, het=True)
        )
        serial += 1
    return "\n".join(lines) + "\nEND\n"


@pytest.mark.parametrize("ligand", ["MSE", "SEC", "NAG", "HOH", "ATP"])
def test_a_free_ligand_does_not_fabricate_an_interface(tmp_path, ligand):
    """A lone MSE/SEC must not invent a binding partner.

    Admitting the two names blind made a single free selenomethionine report an
    eight-residue protein-protein interface, which propagates into
    competition_score and prints a confident false claim that the epitope sits
    in a natural PPI interface. MSE and SEC must behave exactly like NAG here:
    chemically they are amino acids, but nothing bonds them to a chain.
    """
    path = tmp_path / f"{ligand}.pdb"
    path.write_text(_chain_with_free_ligand(ligand), encoding="utf-8")

    assert interfaces.detect_interfaces(str(path), "T") == [], (
        f"a free {ligand} ligand was reported as a protein-protein interface"
    )


@pytest.mark.parametrize("modified", ["MSE", "SEC"])
def test_an_altloc_pair_counts_its_position_once(modified):
    """MET and MSE at one residue number are two Residue objects, one position.

    Their hetflags differ, so Biopython does not merge them. Reporting both
    inflates contact_count, which scout/interfaces.py thresholds as a LIST
    against _MIN_CONTACT_RESIDUES — the filter whose job is rejecting crystal
    contacts. scout/parser.py guards this; these gates must too.
    """
    text = _FC_AB.read_text(encoding="utf-8", errors="replace")
    native = epitope_db._compute_contacts(text, "A", ["B"])
    assert native, "fixture must yield contacts"
    target = native[0]

    partner_lines = [
        "HETATM" + line[6:17] + modified + line[20:]
        for line in text.splitlines()
        if line.startswith("ATOM")
        and len(line) > 26
        and line[21] == "A"
        and line[22:26].strip() == str(target)
    ]
    assert partner_lines, "fixture must have atoms at the target residue"

    lines = text.splitlines()
    dual = "\n".join(lines[:1] + partner_lines + lines[1:]) + "\n"
    after = epitope_db._compute_contacts(dual, "A", ["B"])

    assert len(after) == len(set(after)), (
        f"residue {target} reported twice as a MET/{modified} altloc pair: {after}"
    )
    assert sorted(after) == sorted(native), f"{native} -> {after}"


@pytest.mark.parametrize("modified", ["MSE", "SEC"])
def test_the_partner_chain_gate_keeps_modified_residues(modified):
    """Pins the partner-side gate in scout/interfaces.py specifically.

    Reverting only that gate previously left every test in this module green:
    three of the four gates were pinned, and this one was not. The partner
    chain supplies the atoms that bring target residues within the cutoff, so
    dropping its modified residues shrinks the reported interface.
    """
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")
    native = interfaces.detect_interfaces(str(_FC_DIMER), "A")
    assert native, "fixture must yield an interface"
    native_contacts = sorted(native[0]["contact_residues"])

    partner = native[0]["partner_chain"]

    semet = text
    partner_resnums = sorted(
        {
            int(line[22:26])
            for line in text.splitlines()
            if line.startswith("ATOM") and len(line) > 26 and line[21] == partner
        }
    )
    # Alternating, not all: see the sibling test for why an all-modified chain
    # is deliberately not admitted.
    for resnum in partner_resnums[::2]:
        semet = _respell_as(semet, partner, resnum, modified)

    with tempfile.TemporaryDirectory() as tmpdir:
        probe_path = pathlib.Path(tmpdir) / "partner.pdb"
        probe_path.write_text(semet, encoding="utf-8")
        after = interfaces.detect_interfaces(str(probe_path), "A")

    assert after, f"interface vanished once the partner chain was all {modified}"
    assert sorted(after[0]["contact_residues"]) == native_contacts, (
        f"partner chain spelled {modified} changed the target contacts: "
        f"{native_contacts} -> {sorted(after[0]['contact_residues'])}"
    )


# ---------------------------------------------------------------------------
# Round-two guards. The first version of the free-ligand fix admitted any
# modified residue bonded to its list neighbour, so a free SeMet DIPEPTIDE
# satisfied the test by bonding to itself -- the same phantom interface at a
# different arity. It also left the bond-length constant unpinned: setting it
# to 100 A kept every test green, because no test had a neighbour to measure.
# ---------------------------------------------------------------------------

def _free_peptide_pdb(resname, n_residues, bond_gap=1.25):
    """Target chain T plus a free n-residue peptide of `resname` in chain L.

    The peptide's residues are peptide-bonded to EACH OTHER and to nothing
    else. `bond_gap` sets the C->N spacing so a test can put the link inside or
    outside the accepted range.
    """
    lines, serial = [], 1
    for k in range(8):
        for atom_name in ("N", "CA", "C", "O"):
            lines.append(
                _pdb_atom(serial, atom_name, "ALA", "T", k + 1,
                          3.0 + 0.2 * k, 0.3 * k, 0.4 * k)
            )
            serial += 1
    x = 0.5
    for k in range(n_residues):
        for atom_name, dx in (("N", 0.0), ("CA", 0.4), ("C", 0.8)):
            lines.append(
                _pdb_atom(serial, atom_name, resname, "L", 500 + k,
                          x + dx, 0.2 * k, 0.1 * k, het=True)
            )
            serial += 1
        x += 0.8 + bond_gap
    return "\n".join(lines) + "\nEND\n"


@pytest.mark.parametrize("modified", ["MSE", "SEC"])
@pytest.mark.parametrize("length", [2, 4])
def test_a_free_modified_peptide_does_not_seed_itself(tmp_path, modified, length):
    """A free SeMet peptide must not certify its own residues as polymer.

    Admission seeds from blank-hetflag residues and propagates outward, so a
    run of modified residues attached to nothing real is rejected however long
    it is. Checking only "bonded to a neighbour" let a dipeptide through.
    """
    path = tmp_path / f"{modified}{length}.pdb"
    path.write_text(_free_peptide_pdb(modified, length), encoding="utf-8")

    assert interfaces.detect_interfaces(str(path), "T") == [], (
        f"a free {length}-residue {modified} peptide minted an interface"
    )


def test_the_bond_length_threshold_is_load_bearing(tmp_path):
    """Pins the distance criterion itself, not merely 'has a neighbour'.

    A modified residue sitting a non-bonded distance from the chain must be
    rejected. Without this, the bond cutoff could be raised to 100 A and every
    other test still passed -- and a cutoff above ~2.3 A would let a MET/MSE
    altloc pair certify itself, reintroducing the double-count.
    """
    text = _FC_AB.read_text(encoding="utf-8", errors="replace")
    native = epitope_db._compute_contacts(text, "A", ["B"])
    target = native[0]

    # Re-spell a real contact as MSE, then translate it far enough that no
    # backbone bond survives while it still sits inside the 4.5 A contact shell
    # of its partner. It must drop out: it is no longer attached to the chain.
    out = []
    for line in text.splitlines():
        if (line.startswith("ATOM") and len(line) > 54 and line[21] == "A"
                and line[22:26].strip() == str(target)):
            x = float(line[30:38]) + 3.0
            line = ("HETATM" + line[6:17] + "MSE" + line[20:30]
                    + f"{x:8.3f}" + line[38:])
        out.append(line)

    after = epitope_db._compute_contacts("\n".join(out) + "\n", "A", ["B"])
    assert target not in after, (
        f"residue {target} was translated off its backbone bond but still "
        f"counted as a polymer residue: {after}"
    )


def test_a_ca_only_trace_still_admits_chain_modified_residues(tmp_path):
    """Low-resolution CA-only structures must not silently lose their MSE.

    Rejecting on missing backbone C/N made the whole fix inert on CA-only
    traces -- real entries exist -- where a chain link and a free ligand are
    still perfectly distinguishable, because consecutive CA atoms sit ~3.8 A
    apart and a stray ligand does not.
    """
    lines, serial = [], 1
    for k in range(6):
        resname = "MSE" if k == 3 else "ALA"
        lines.append(
            _pdb_atom(serial, "CA", resname, "T", k + 1, 3.8 * k, 0.0, 0.0,
                      het=(resname == "MSE"))
        )
        serial += 1
    for k in range(6):
        lines.append(_pdb_atom(serial, "CA", "ALA", "P", 100 + k,
                               3.8 * k, 4.0, 0.0))
        serial += 1

    path = tmp_path / "ca_only.pdb"
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")

    from scout import polymer
    from Bio.PDB import PDBParser

    chain = PDBParser(QUIET=True).get_structure("x", str(path))[0]["T"]
    kept = [r.get_id()[1] for r in polymer.polymer_residues(chain)]
    assert kept == [1, 2, 3, 4, 5, 6], (
        f"CA-only trace lost its chain MSE: {kept}"
    )


def test_insertion_coded_residues_are_distinct_positions(tmp_path):
    """100 and 100A are two residues and must both contribute atoms.

    The dedupe key is (resseq, icode). Keying on resseq alone still passed
    every other test -- no fixture in this repo carries an insertion code --
    while silently collapsing a Kabat-numbered CDR loop, discarding one
    residue's atoms entirely. The reported list cannot distinguish them (the
    callers emit the sequence number alone), which is exactly why the atom-level
    assertion below is the one that has to hold.
    """
    from Bio.PDB import PDBParser
    from scout import polymer

    lines, serial = [], 1
    for resnum, icode in ((99, " "), (100, " "), (100, "A"), (100, "B"), (101, " ")):
        for atom_name, dx in (("N", 0.0), ("CA", 0.4), ("C", 0.8)):
            line = _pdb_atom(serial, atom_name, "ALA", "T", resnum,
                             3.6 * serial / 3.0 + dx, 0.0, 0.0)
            line = line[:26] + icode + line[27:]   # column 27 is the icode
            lines.append(line)
            serial += 1
    path = tmp_path / "icode.pdb"
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")

    chain = PDBParser(QUIET=True).get_structure("x", str(path))[0]["T"]
    kept = polymer.polymer_residues(chain)
    positions = [(r.get_id()[1], r.get_id()[2]) for r in kept]

    assert len(kept) == 5, f"insertion codes collapsed: {positions}"
    assert positions == [(99, " "), (100, " "), (100, "A"), (100, "B"), (101, " ")]
