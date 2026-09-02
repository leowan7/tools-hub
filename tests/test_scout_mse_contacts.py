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

import io
import pathlib
import tempfile

import pytest

from scout import epitope_db, interfaces, polymer

pytest.importorskip("Bio")
pytest.importorskip("numpy")

_EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "static" / "example"
_FC_AB = _EXAMPLES / "3s7g_fc_ab.pdb"
_FC_DIMER = _EXAMPLES / "3ave_igg1_fc_dimer.pdb"

NEWLINE = chr(10)  # heredoc-safe; literal escapes get mangled in this repo's tooling


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
    sequence; the code uses min(). PDB chains are usually domain constructs
    shorter than the full-length UniProt entry, so max would reject them all at
    the 0.70 threshold.

    An earlier version of this docstring went on to conclude that min() is
    therefore why the MSE gate in _extract_chain_sequence could not flip that
    threshold. It does not follow -- the denominator says nothing about how
    many matches a deletion costs, which can be zero or more than one -- and
    the evidence behind it was a sequence compared against its own MET-ized
    self, pinned at 1.0000 by construction. Measured against real UniProt
    references the gate moved the reported identity on 61 of 173 SeMet chains.
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


@pytest.mark.parametrize("modelled,label", [
    (("CA",), "CA only"),
    (("CA", "C"), "CA and C, no N"),
    (("N", "CA"), "N and CA, no C"),
    (("N", "CA", "C"), "full backbone"),
])
def test_admission_is_monotone_in_backbone_completeness(modelled, label):
    """Adding a backbone atom must never turn an admitted residue into a drop.

    _bond_length once returned the first finite C->N measurement it found. For
    a CA-only MSE both directions raised and the CA fallback admitted it; ADDING
    the C atom let the reverse direction measure -- C(i+1)...N(i), which runs
    4.1-6.1 A on real bonded pairs -- and that is finite, so the short-circuit
    returned it and the residue was dropped. More information, worse answer.
    Partial backbones occur in deposited data and in user uploads.
    """
    import io

    from Bio.PDB import PDBParser

    from scout import polymer

    lines, serial = [], 1
    for atom_name, dx in (("N", 0.0), ("CA", 1.4), ("C", 2.5), ("O", 2.6)):
        lines.append(_pdb_atom(serial, atom_name, "ALA", "A", 1, dx, 0.0, 0.0))
        serial += 1
    for atom_name in modelled:
        dx = {"N": 3.83, "CA": 5.2, "C": 6.3}[atom_name]
        lines.append(
            _pdb_atom(serial, atom_name, "MSE", "A", 2, dx, 0.0, 0.0, het=True)
        )
        serial += 1

    chain = PDBParser(QUIET=True).get_structure(
        "mono", io.StringIO("\n".join(lines) + "\nEND\n")
    )[0]["A"]
    kept = [r.get_id()[1] for r in polymer.polymer_residues(chain)]

    assert 2 in kept, (
        f"a peptide-bonded MSE modelled with {label} was dropped: kept {kept}"
    )


# ---------------------------------------------------------------------------
# Round-three guards. Independent mutation testing found 41 survivors, and one
# structural cause behind most of them: every assertion in the interface tests
# above compares `after` to `native` with BOTH sides computed by the same code.
# A relative comparison cannot see a uniform shift, so deleting the target-chain
# gate entirely -- publishing 15 water molecules as interface contacts on a
# checked-in fixture -- passed the whole module. The assertions below are
# ABSOLUTE: they pin what the output must never contain, independent of what
# the same run produced elsewhere.
# ---------------------------------------------------------------------------

def test_no_interface_contact_is_ever_a_heteroatom():
    """Absolute guard: contact_residues must contain no water, ion or ligand.

    Biopython puts a chain's waters and ions in the SAME Chain object as its
    polymer, so they are one unguarded `list()` away from the published list.
    Chain A of this fixture carries 140 HOH and a ZN. With the target-chain
    gate removed, contact_count went 28 -> 43 and every addition was a water,
    while every relative assertion in this module still passed.
    """
    from Bio.PDB import PDBParser

    chain = PDBParser(QUIET=True).get_structure("f", str(_FC_DIMER))[0]["A"]
    heteroatom_resnums = {r.get_id()[1] for r in chain if r.get_id()[0] != " "}
    assert heteroatom_resnums, "fixture must carry heteroatoms for this to test anything"

    interfaces_found = interfaces.detect_interfaces(str(_FC_DIMER), "A")
    assert interfaces_found, "fixture must yield an interface"

    reported = set(interfaces_found[0]["contact_residues"])
    leaked = sorted(reported & heteroatom_resnums)
    assert not leaked, f"heteroatoms reported as interface contacts: {leaked}"

    # Every reported number must belong to a residue the gate actually admits.
    admitted = {r.get_id()[1] for r in polymer_residues_of(chain)}
    assert reported <= admitted, (
        f"contacts not admitted by the polymer gate: {sorted(reported - admitted)}"
    )


def polymer_residues_of(chain):
    from scout import polymer

    return polymer.polymer_residues(chain)


def test_a_c_terminal_modified_residue_survives_the_forward_sweep(tmp_path):
    """A chain-final MSE followed by a ligand needs the FORWARD sweep.

    Admission propagates in two passes. Removing the BACKWARD sweep was pinned;
    removing the FORWARD one was not, and it is the one a C-terminal SeMet
    depends on: its right-hand neighbour in the Biopython chain is the ZN or
    HOH tail that a normal PDB chain ends with, so only the left-to-right pass
    can reach it. C-terminal selenomethionine is common.
    """
    from Bio.PDB import PDBParser

    from scout import polymer

    lines, serial = [], 1
    for k in range(4):                       # canonical run, seeds admission
        for atom_name, dx in (("N", 0.0), ("CA", 1.4), ("C", 2.5)):
            lines.append(_pdb_atom(serial, atom_name, "ALA", "A", k + 1,
                                   3.8 * k + dx, 0.0, 0.0))
            serial += 1
    for atom_name, dx in (("N", 0.0), ("CA", 1.4), ("C", 2.5)):   # C-terminal MSE
        lines.append(_pdb_atom(serial, atom_name, "MSE", "A", 5,
                               3.8 * 4 + dx, 0.0, 0.0, het=True))
        serial += 1
    lines.append(_pdb_atom(serial, "ZN", " ZN", "A", 900, 40.0, 0.0, 0.0, het=True))

    path = tmp_path / "cterm.pdb"
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")
    chain = PDBParser(QUIET=True).get_structure("c", str(path))[0]["A"]
    kept = [r.get_id()[1] for r in polymer.polymer_residues(chain)]

    assert 5 in kept, f"C-terminal MSE dropped; only the forward sweep reaches it: {kept}"
    assert 900 not in kept, f"the ZN tail was admitted: {kept}"


def test_the_ca_trace_scale_has_an_upper_bound(tmp_path):
    """A CA-only free ligand far from the chain must still be rejected.

    _CA_TRACE_SCALE was pinned only from below: 1.9, 3.0 and 100.0 all passed,
    and at 100 a free MSE sitting 12 A from a CA-only chain is admitted --
    the phantom-interface defect this module exists to prevent, restored on the
    CA-only path. The constant appeared in no test in the repo.
    """
    from Bio.PDB import PDBParser

    from scout import polymer

    lines, serial = [], 1
    for k in range(5):                              # CA-only trace, 3.8 A apart
        lines.append(_pdb_atom(serial, "CA", "ALA", "A", k + 1, 3.8 * k, 0.0, 0.0))
        serial += 1
    # Free MSE, CA only, 12 A past the end of the chain: no bond, at any scale
    # a sane threshold admits.
    lines.append(_pdb_atom(serial, "CA", "MSE", "A", 600,
                           3.8 * 4 + 12.0, 0.0, 0.0, het=True))

    path = tmp_path / "ca_ligand.pdb"
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")
    chain = PDBParser(QUIET=True).get_structure("l", str(path))[0]["A"]
    kept = [r.get_id()[1] for r in polymer.polymer_residues(chain)]

    assert kept == [1, 2, 3, 4, 5], (
        f"a free CA-only MSE 12 A from the chain was admitted: {kept}"
    )


def test_the_two_contact_implementations_agree_exactly():
    """Cross-check that pins residue IDENTITY, not membership.

    scout/interfaces.py and scout/epitope_db.py compute contacts over the same
    geometry by separate code. On this fixture they agree residue for residue,
    so a shift in either -- an off-by-one on the emitted number, a CA-only
    contact sphere -- breaks the equality.

    It does NOT catch publishing the partner's list instead of the target's:
    this fixture is a symmetric homodimer with shared numbering, so those two
    lists are byte-identical and the swap is unobservable here. The sibling
    test below covers that on renumbered chains. A subset or "unchanged since last run" assertion
    cannot see any of those: shifting every number by one keeps it inside the
    admitted set, which is how an off-by-one survived the whole module.

    Chain A of this fixture carries 141 HETATM records (448 across all four
    chains), so a missing gate on EITHER side shows up here too.
    """
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")

    by_epitope_db = sorted(epitope_db._compute_contacts(text, "A", ["B"]))
    detected = interfaces.detect_interfaces(str(_FC_DIMER), "A")
    assert detected, "fixture must yield an interface"
    by_interfaces = sorted(detected[0]["contact_residues"])

    assert by_epitope_db, "fixture must yield contacts"
    assert by_epitope_db == by_interfaces, (
        "the two contact implementations disagree:\n"
        f"  epitope_db:  {by_epitope_db}\n"
        f"  interfaces:  {by_interfaces}"
    )


def test_antibody_chain_heteroatoms_do_not_widen_the_contact_sphere():
    """Absolute guard on the ANTIBODY-side gate, without magic numbers.

    That gate builds the atom cloud defining the contact sphere, so dropping it
    admits the partner chain's waters and ions and mints spurious antigen
    contacts. It had no coverage at all: the only fixture exercising it carries
    ZERO HETATM records, so deleting the gate there is a no-op.

    The check is that the antigen contacts are unchanged when the partner
    chain's heteroatoms are physically removed from the input. If the gate is
    doing its job they are already excluded and stripping them changes nothing;
    if it is not, the full file reports more contacts than the stripped one.
    """
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")

    stripped = NEWLINE.join(
        line for line in text.splitlines()
        if not (line.startswith("HETATM") and len(line) > 21 and line[21] == "B")
    ) + NEWLINE
    assert stripped != text, "fixture must carry heteroatoms on the partner chain"

    with_het = sorted(epitope_db._compute_contacts(text, "A", ["B"]))
    without_het = sorted(epitope_db._compute_contacts(stripped, "A", ["B"]))

    assert with_het, "fixture must yield contacts"
    assert with_het == without_het, (
        "the partner chain's heteroatoms changed the antigen contact list, so "
        "they are entering the contact sphere: "
        f"with {with_het} / without {without_het} "
        f"  extra:   {sorted(set(with_het) - set(without_het))}"
    )


def test_the_target_and_partner_contact_lists_are_not_interchangeable(tmp_path):
    """Publishing the partner's residue numbers as the target's must be caught.

    The cross-check above cannot see this: 3ave is a symmetric homodimer whose
    two chains share numbering, so the target and partner lists are identical
    and swapping them changes nothing. Renumbering chain B by +1000 -- geometry
    untouched -- separates them, and the swap becomes observable.

    contact_residues is rendered and unioned into the scored contact set, so on
    any hetero-complex (the real use case: antigen plus antibody) the swap
    publishes the wrong chain's numbers to the user and into a score.
    """
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")
    renumbered = []
    for line in text.splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) > 26 and line[21] == "B":
            line = line[:22] + "%4d" % (int(line[22:26]) + 1000) + line[26:]
        renumbered.append(line)

    path = tmp_path / "renumbered.pdb"
    path.write_text(NEWLINE.join(renumbered) + NEWLINE, encoding="utf-8")

    detected = interfaces.detect_interfaces(str(path), "A")
    assert detected, "renumbering must not destroy the interface"
    reported = sorted(detected[0]["contact_residues"])

    assert reported, "fixture must yield contacts"
    assert max(reported) < 1000, (
        "contact_residues carries chain B's renumbered residues, so the "
        "partner list is being published as the target's: %r" % (reported,)
    )


def test_glycan_insertion_codes_are_distinct_positions():
    """The glycan dedupe must key on (resseq, icode), not resseq alone.

    Keying on the sequence number collapses a Kabat-numbered CDR run -- 100,
    100A, 100B -- into one residue, which both drops residues and shifts the
    positional i, i+1, i+2 window the sequon scan depends on. The equivalent
    guard exists for scout/polymer.py; this one did not, and the mutation
    survived green.
    """
    import io

    from Bio.PDB import PDBParser

    from scout.glycan import detect_glycosylation_sequons

    lines, serial = [], 1
    for resname, resnum, icode in (
        ("ALA", 100, " "), ("ASN", 100, "A"), ("GLY", 100, "B"), ("THR", 101, " ")
    ):
        for atom_name in ("N", "CA", "C", "O", "CB"):
            line = _pdb_atom(serial, atom_name, resname, "A", resnum,
                             3.8 * serial / 5.0, 0.0, 0.0)
            line = line[:26] + icode + line[27:]
            lines.append(line)
            serial += 1

    chain = PDBParser(QUIET=True).get_structure(
        "kabat", io.StringIO(NEWLINE.join(lines) + NEWLINE + "END" + NEWLINE)
    )[0]["A"]
    found = detect_glycosylation_sequons(chain)

    assert [(f["resnum"], f["motif"]) for f in found] == [(100, "N-G-T")], (
        "insertion-coded residues collapsed, losing the sequon: %r" % (found,)
    )


def test_the_ca_proxy_does_not_override_a_measurement_that_was_taken():
    """A free ligand at non-bonded Ca spacing stays out, either way round.

    _bond_length consults Ca--Ca when the forward C->N pair is unmeasurable.
    With the scale at 2.1 that accepted Ca--Ca up to 4.2 A, so a free MSE
    sitting 4.0 A from a chain end -- a distance no peptide bond produces --
    was admitted even though the reverse C->N had been measured at 6.17 A and
    said otherwise. The commit that introduced this was titled for stopping
    exactly that, and stopped only the mirror case.

    The fix is not to trust the reverse measurement, which runs 4.1-6.1 A on
    genuinely bonded pairs and so cannot discriminate. It is that the Ca proxy
    was too loose: trans-peptide Ca--Ca is 3.78-3.82 A, and the ceiling is now
    3.9 A rather than 4.2 A.
    """
    import io

    from Bio.PDB import PDBParser

    from scout import polymer

    text = (
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00 0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00 0.00           C\n"
        "ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00 0.00           C\n"
        "HETATM  900  CA  MSE A 501       5.458   0.000   0.000  1.00 0.00           C\n"
        "HETATM  901  C   MSE A 501       6.009   1.420   0.000  1.00 0.00           C\n"
        "END\n"
    )
    chain = PDBParser(QUIET=True).get_structure("p", io.StringIO(text))[0]["A"]
    kept = [r.get_id()[1] for r in polymer.polymer_residues(chain)]

    assert 501 not in kept, (
        f"a free MSE 4.0 A from the chain, with the reverse C->N measured at "
        f"6.17 A, was admitted on the Ca proxy: {kept}"
    )
    assert kept == [1], f"the real residue must survive: {kept}"


def test_the_reverse_direction_is_never_consulted():
    """C(i+1)...N(i) must play no part in admission.

    It runs 4.1-6.1 A on genuinely bonded pairs, so it can never rescue a real
    bond, and consulting it produced three separate defects: non-monotone
    admission, a CA proxy overriding a measurement that was taken, and this --
    a free ligand 6.0 A forward and 8.0 A Ca--Ca admitted because its C landed
    1.5 A from the previous residue's N, which is a clash, not a bond.

    Restoring the reverse term as a min, in any of its forms, admits residue
    500 here.
    """
    import io

    from Bio.PDB import PDBParser

    from scout import polymer

    text = (
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00 0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00 0.00           C\n"
        "ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00 0.00           C\n"
        "HETATM  900  N   MSE A 500       8.009   1.420   0.000  1.00 0.00           N\n"
        "HETATM  901  CA  MSE A 500       9.458   0.000   0.000  1.00 0.00           C\n"
        "HETATM  902  C   MSE A 500       0.000   1.500   0.000  1.00 0.00           C\n"
        "END\n"
    )
    chain = PDBParser(QUIET=True).get_structure("rev", io.StringIO(text))[0]["A"]
    kept = [r.get_id()[1] for r in polymer.polymer_residues(chain)]

    assert kept == [1], (
        f"a free ligand was admitted on a reverse C...N contact of 1.5 A, "
        f"with the forward pair at 6.0 A and Ca--Ca at 8.0 A: {kept}"
    )


def test_the_backward_sweep_passes_its_residues_upstream_first():
    """Both sweeps must call _bond_length as (upstream, downstream).

    The whole forward-only gate rests on that argument order, and only the
    forward sweep was pinned. Reversing the BACKWARD sweep's arguments admits
    an N-terminal free ligand: its own C sits near the following residue's N,
    which is the reverse pair, and reading it as the forward one calls that a
    bond.
    """
    import io

    from Bio.PDB import PDBParser

    from scout import polymer

    # Free MSE FIRST in the chain, so only the backward sweep can reach it.
    # Forward (MSE.C -> ALA.N) is 6.0 A; the reverse pair is short.
    text = (
        "HETATM  900  CA  MSE A 500       0.000   0.000   0.000  1.00 0.00           C\n"
        "HETATM  901  C   MSE A 500       6.000   1.420   0.000  1.00 0.00           C\n"
        "ATOM      1  N   ALA A   1       3.850   0.000   0.000  1.00 0.00           N\n"
        "ATOM      2  CA  ALA A   1       5.308   0.000   0.000  1.00 0.00           C\n"
        "ATOM      3  C   ALA A   1       5.859   1.420   0.000  1.00 0.00           C\n"
        "ATOM      4  N   ALA A   2       7.700   0.000   0.000  1.00 0.00           N\n"
        "ATOM      5  CA  ALA A   2       9.158   0.000   0.000  1.00 0.00           C\n"
        "ATOM      6  C   ALA A   2       9.709   1.420   0.000  1.00 0.00           C\n"
        "END\n"
    )
    chain = PDBParser(QUIET=True).get_structure("bwd", io.StringIO(text))[0]["A"]
    kept = [r.get_id()[1] for r in polymer.polymer_residues(chain)]

    assert 500 not in kept, (
        f"the backward sweep admitted an N-terminal free ligand, so it is "
        f"measuring the reverse pair as though it were the bond: {kept}"
    )
# ---------------------------------------------------------------------------
# _extract_chain_sequence: the same gate, but the consequence is a WELD.
#
# The four gates above lose a residue from a list. This one loses a LETTER from
# a string, and a string closes the gap: re-spell 3ave chain A's MET 252 as a
# HETATM MSE and KDTL-M-ISRT becomes KDTLISRT, which is not the sequence of that
# chain. It then goes to _sequence_identity against UniProt, and the result is
# rendered to the user as sequence_identity_pct.
#
# (KDTLISRT is a perfectly ordinary octamer that does occur in real proteins --
# an earlier draft of this comment claimed it could not, on no evidence. What
# makes it wrong is only that it is not THIS chain.)
#
# Measured against real UniProt references on 90 SeMet depositions. 223 chains
# carried an MSE or SEC, 196 of those also resolved a DBREF accession, and 173
# were scored -- the other 23 returned no UniProt sequence. Across those 173,
# with 1030 residues recovered, the reported identity moved on 61 chains, in
# both directions, median 0.06 pp. The 4.0 pp maximum is a SINGLE deposition,
# 3BKD, whose eight copies in the asymmetric unit are all eight of the extreme
# rows and set the p90 as well; treat it as one observation, not eight.
#
# The earlier "benign" verdict on this site was measured by comparing the gated
# sequence against its own MET-ized self, which forces exactly 1.0000 because
# one string is a subsequence of the other and the denominator is min(len).
# That number could not have come out any other way.
#
# Against the pre-fix gate this file goes 2 red / 26 green, and only the two
# arms of test_a_modified_residue_stays_in_the_extracted_sequence are red --
# they are the whole of the evidence for this change. The others answer a
# different question, "would a plausible wrong version of the fix be caught",
# and each names the mutant that kills it:
#
#   ...altloc_pair_contributes_one_letter  <- deleting polymer.py's dedupe
#   ...residue_numbers_stay_parallel...    <- appending the number before the
#                                             _THREE_TO_ONE lookup, not after
#   ...welds_its_neighbours                <- nothing; non-vacuity floor
#   ...welded_subsequence...perfect...     <- nothing; pins a docstring claim
#
# The first two were green against their own mutants when first written, and an
# independent review caught that. Re-check them the same way before trusting
# them: a guard that cannot fail looks exactly like one that passed.
# ---------------------------------------------------------------------------

# 3ave carries NO selenium: it has zero MSE records and its only MODRES lines
# are the ASN 297 glycosylation sites. Residue 252 of chain A is an ordinary
# MET with four ordinary residues either side, which is what makes it usable --
# re-spelling it is a pure record-type change, and dropping it is a visible
# weld. Read "MSE 252" anywhere below as "the MET at 252, re-spelled".
_MET_RESNUM = 252
_MET_CONTEXT = "KDTLMISRT"
_WELDED_CONTEXT = "KDTLISRT"
# A residue number no Fc chain uses, for the nucleotide spliced in below.
_UNMAPPED_RESNUM = 9001


def _extract(tmp_path, text, chain_id="A"):
    path = tmp_path / "chain.pdb"
    path.write_text(text, encoding="utf-8")
    return epitope_db._extract_chain_sequence(path, chain_id)


def _drop_residue(text, chain, resnum):
    return "\n".join(
        line for line in text.splitlines()
        if not (
            line.startswith(("ATOM", "HETATM"))
            and len(line) > 26
            and line[21] == chain
            and line[22:26].strip() == str(resnum)
        )
    ) + "\n"


@pytest.mark.parametrize("modified,letter", [("MSE", "M"), ("SEC", "C")])
def test_a_modified_residue_stays_in_the_extracted_sequence(tmp_path, modified, letter):
    """Re-spelling one residue may change its letter, never the sequence length.

    _THREE_TO_ONE has mapped MSE->M and SEC->C for as long as it has existed,
    so the hetflag gate that used to sit above it contradicted the map two
    lines down. Not for every spelling, though: an ATOM-spelled MSE carries a
    blank hetflag and did reach the lookup, exactly as the module header says of
    1CC1's SEC 492. It is the HETATM spelling -- the one real depositions use --
    that the gate could never let through.
    """
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")
    base_nums, base_seq = _extract(tmp_path, text)
    index = base_nums.index(_MET_RESNUM)
    assert base_seq[index] == "M", "fixture must start with a MET at this position"

    nums, seq = _extract(tmp_path, _respell_as(text, "A", _MET_RESNUM, modified))

    assert nums == base_nums, (
        f"{modified} {_MET_RESNUM} dropped out of the residue numbers"
    )
    assert seq == base_seq[:index] + letter + base_seq[index + 1:], (
        f"{modified} {_MET_RESNUM} changed the sequence beyond its own letter"
    )


def test_dropping_that_residue_welds_its_neighbours(tmp_path):
    """Non-vacuity floor, and the defect made concrete.

    The arm above asserts a sequence does NOT change, which proves nothing
    unless removing the residue provably does change it. This is what the
    hetflag gate used to produce for a HETATM-spelled MSE at this position.
    """
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")
    _, base_seq = _extract(tmp_path, text)
    assert _MET_CONTEXT in base_seq

    nums, seq = _extract(tmp_path, _drop_residue(text, "A", _MET_RESNUM))

    assert _MET_RESNUM not in nums
    assert len(seq) == len(base_seq) - 1
    assert _WELDED_CONTEXT in seq and _MET_CONTEXT not in seq, (
        "the gap did not close, so this fixture cannot detect a weld"
    )


def test_the_residue_numbers_stay_parallel_to_the_sequence(tmp_path):
    """The invariant the weld violates, stated so a future edit trips over it.

    _extract_chain_sequence returns two lists that callers may zip. They are
    appended in one loop today, on the far side of a `continue` that fires when
    _THREE_TO_ONE has no entry for the residue.

    That branch is what makes the invariant pinnable, and it is UNREACHABLE on
    the Fc fixture alone: all 211 of its admitted residues map, so appending the
    number before the lookup instead of after it goes unnoticed. polymer_residues
    admits blank-hetflag nucleotides by design -- its own docstring says so --
    so one spliced DA reaches the branch and a desync becomes a length mismatch.
    An earlier version of this test omitted the DA and was killed by no mutant
    in the whole suite.

    len(nums) == len(seq) is the only assertion here, deliberately. That version
    also asserted the numbers were sorted and unique, and BOTH were properties
    of this fixture rather than of the code: polymer_residues returns chain
    order, not sorted order, and it dedupes on (resseq, icode) while callers
    emit only resseq -- so an insertion-coded chain legitimately returns
    [99, 100, 100, 100, 101]. test_insertion_coded_residues_are_distinct_
    positions above pins that on purpose. Asserting the opposite here passed
    only because 3ave has no insertion codes.
    """
    from Bio.PDB import PDBParser

    lines = _respell_as(_FC_DIMER.read_text(encoding="utf-8", errors="replace"),
                        "A", _MET_RESNUM, "MSE").splitlines()
    at = next(
        i for i, line in enumerate(lines)
        if line.startswith(("ATOM", "HETATM"))
        and len(line) > 26
        and line[21] == "A"
        and line[22:26].strip() == str(_MET_RESNUM)
    )
    nucleotide = [
        _pdb_atom(90000 + k, name, "DA", "A", _UNMAPPED_RESNUM, 90.0 + k, 90.0, 90.0)
        for k, name in enumerate(("P", "C1", "N9"))
    ]
    spliced = "\n".join(lines[:at] + nucleotide + lines[at:]) + "\n"

    # Non-vacuity: the spliced residue must actually REACH the lookup, or this
    # is back to asserting a branch nothing executes.
    assert epitope_db._THREE_TO_ONE.get("DA") is None
    chain = PDBParser(PERMISSIVE=True, QUIET=True).get_structure(
        "x", io.StringIO(spliced))[0]["A"]
    assert any(r.get_id()[1] == _UNMAPPED_RESNUM for r in polymer.polymer_residues(chain)), (
        "polymer_residues no longer admits the spliced nucleotide, so the "
        "aa-is-None branch is unreachable and this test proves nothing"
    )

    nums, seq = _extract(tmp_path, spliced)

    assert _UNMAPPED_RESNUM not in nums, "an unmapped residue reached the numbers"
    assert len(nums) == len(seq) > 0


@pytest.mark.parametrize("modified", ["MSE", "SEC"])
def test_an_altloc_pair_contributes_one_letter(tmp_path, modified):
    """The defect a bare whitelist would introduce on THIS consumer.

    Admitting the two names without deduping emits a MET/MSE altloc pair twice,
    which lengthens the sequence and moves the identity denominator. The
    contacts test above pins the same guard against a list; a string needs its
    own arm because the failure looks different.

    The copy must be spliced ADJACENT to the residue it pairs with. Prepending
    it at the top of the file -- what the contacts test does, and gets away with
    because its target sits near the chain start -- lands it beside LEU 234
    here, where the connectivity gate rejects it outright and the dedupe is
    never reached. Reviewed as green against a deleted dedupe before this was
    corrected.

    Which LETTER survives is deliberately not asserted. The dedupe keeps the
    copy that comes first in the file, so a MET/SEC pair reads C and a MET/MSE
    pair reads M -- and only the second is a real thing, since SEC substitutes
    for CYS, not MET. One position, one letter, and nothing else disturbed is
    the property that holds for both.
    """
    text = _FC_DIMER.read_text(encoding="utf-8", errors="replace")
    base_nums, base_seq = _extract(tmp_path, text)

    lines = text.splitlines()
    hits = [
        (i, "HETATM" + line[6:17] + modified + line[20:])
        for i, line in enumerate(lines)
        if line.startswith("ATOM")
        and len(line) > 26
        and line[21] == "A"
        and line[22:26].strip() == str(_MET_RESNUM)
    ]
    assert hits, "fixture must have atoms at the target residue"
    at = hits[0][0]
    partner = [line for _, line in hits]
    nums, seq = _extract(tmp_path, "\n".join(lines[:at] + partner + lines[at:]) + "\n")

    index = base_nums.index(_MET_RESNUM)
    assert nums == base_nums, f"altloc pair emitted {len(nums)} positions"
    assert len(seq) == len(base_seq), "the altloc pair added a letter"
    assert seq[:index] == base_seq[:index] and seq[index + 1:] == base_seq[index + 1:], (
        "the altloc pair disturbed the sequence beyond its own position"
    )


def test_a_welded_subsequence_can_still_score_a_perfect_identity():
    """Pins the caveat in _sequence_identity's docstring.

    Matching BLOCKS are counted, not aligned positions, so a deletion CAN be
    free: a string absent from the reference still scores 1.0000. That is how
    the gate above stayed invisible -- a perfect score means "no evidence of
    mismatch", not "identical".

    "Can", not "does". A deletion that splits a repeated motif costs more than
    the residue removed: DECDEDE -> DEDEDE scores 0.6667, and on 2ISB chain A
    against O29167 one deletion cost two matches (1.0000 -> 0.9944). Five of
    the 173 measured chains behave that way. An earlier draft of this docstring
    and of _sequence_identity's stated the flat version and was wrong.
    """
    assert _WELDED_CONTEXT not in _MET_CONTEXT
    assert epitope_db._sequence_identity(_MET_CONTEXT, _WELDED_CONTEXT) == pytest.approx(1.0)

    # The counterexample, pinned so the hedge above cannot quietly become false.
    assert epitope_db._sequence_identity("DECDEDE", "DEDEDE") < 0.7
