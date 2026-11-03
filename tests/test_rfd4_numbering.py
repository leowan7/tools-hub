"""Author numbering vs mmCIF ``label_seq_id`` for the rfd4 tool.

The failure mode this file exists to make impossible is a run that addresses the
WRONG residue and completes normally
(``docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md`` §10 item 5). A count cannot
see that, so where the mapping's answer is what is under test the assertion names
a specific residue -- its author token and its ``label_seq_id``, plus its residue
name where the name is part of the point. Other tests here assert a refusal, or
a count or range where that is
itself the property (how many rows the CIF carries, what a chain's label range
is). Read each test's own assertions; this docstring does not try to tally
them.

The fixtures are real deposited structures, trimmed:

* ``static/example/1HEW.pdb`` -- hen lysozyme, one POLYMER chain, author
  1..129, gapless; a second author chain B carries 3 NAG, and chain A carries
  103 waters.
  The EASY case, where author numbering and ``label_seq_id`` coincide. Present so
  that the identity-mapping check below has something to be indistinguishable on.
* ``static/example/3ave_igg1_fc_dimer.pdb`` -- IgG1 Fc homodimer, chains A and B,
  author numbering starting at 234 and 237. Non-1-based, and the two protomers
  share one author numbering.
* ``tests/fixtures/rfd4/1igt_fv_trimmed.pdb`` -- trimmed from RCSB entry 1IGT
  (intact mouse IgG2a). Chain A author 1..40; chains B and D author 45..113 in
  Kabat numbering, so they carry insertion codes (52A, 82A/B/C, 100H..100K) and
  an author gap (97 -> 100H), and B and D are two protomers under one numbering.

Not covered, deliberately: whether upstream's reader agrees with the CIF this
module writes. That needs the container and is §8 step 2's gate. Nothing in this
file has been through ``rfd4-generate``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.rfd4.numbering import (
    AuthorResidue,
    NumberingError,
    ResidueEntry,
    ResidueMapping,
    _CCD_COVERED_RESIDUES,
    _MODRES_EQUIV,
    _WATER_RESNAMES,
    _cif_value,
    _is_polymer,
    format_hotspot_selection,
    hotspot_atoms_for_residue,
    label_range_for_chain,
    pdb_to_cif,
)

REPO = Path(__file__).resolve().parent.parent
LYSOZYME = REPO / "static" / "example" / "1HEW.pdb"
FC_DIMER = REPO / "static" / "example" / "3ave_igg1_fc_dimer.pdb"
IGG_FV = REPO / "tests" / "fixtures" / "rfd4" / "1igt_fv_trimmed.pdb"


def _convert(path: Path):
    return pdb_to_cif(path.read_text(errors="replace"))


# ---------------------------------------------------------------------------
# What label_seq_id is
# ---------------------------------------------------------------------------

def test_label_seq_id_is_the_within_chain_file_order_index():
    """Each chain's polymer residues are numbered 1..n in file order."""
    _cif, mapping = _convert(IGG_FV)
    for chain in ("A", "B", "D"):
        labels = [e.label_seq_id for e in mapping.polymer_entries(chain)]
        assert labels == list(range(1, len(labels) + 1)), chain


def test_the_easy_case_coincides_so_it_cannot_prove_anything():
    """Lysozyme is 1-based and gapless: the two numberings are equal throughout.

    Asserted so the identity-mapping check below is honest about which fixture
    is and is not evidence.
    """
    _cif, mapping = _convert(LYSOZYME)
    assert all(e.label_seq_id == e.author.seq for e in mapping.polymer_entries())
    assert mapping.diverges() is False
    # ... and the specific residue identity, both ways.
    assert mapping.label_for("A", 57) == ("A", 57)
    assert mapping.author_for("A", 57) == AuthorResidue("A", 57, "")


# ---------------------------------------------------------------------------
# Divergence case 1: non-1-based author numbering
# ---------------------------------------------------------------------------

def test_non_one_based_author_numbering_offsets_every_residue():
    """Fc chain A starts at author 234, so label_seq_id is author - 233."""
    _cif, mapping = _convert(FC_DIMER)
    assert mapping.diverges() is True

    first = mapping.polymer_entries("A")[0]
    assert (first.author.seq, first.label_seq_id) == (234, 1)

    # The specific residue a customer would pick: Fc position 297 (the N-glycan
    # site neighbourhood) is NOT label_seq_id 297.
    assert mapping.label_for("A", 297) == ("A", 64)
    assert mapping.author_for("A", 64) == AuthorResidue("A", 297, "")
    # And the residue that label_seq_id 297 would address does not exist, so a
    # pipeline that sent the author number unchanged would be refused here
    # rather than silently addressing something else.
    with pytest.raises(NumberingError):
        mapping.author_for("A", 297)


def test_the_resname_at_a_mapped_position_is_the_resname_the_author_id_names():
    """Identity, not position: the CIF row at the mapped id is the right residue."""
    cif, mapping = _convert(FC_DIMER)
    entry = next(e for e in mapping.polymer_entries("A") if e.author.seq == 297)
    asym, label = mapping.label_for("A", 297)

    rows = [
        line.split()
        for line in cif.splitlines()
        if line.startswith(("ATOM", "HETATM"))
    ]
    # _atom_site column order is fixed by `_ATOM_SITE_FIELDS`.
    matched = {
        (r[5], r[15]) for r in rows if r[6] == asym and r[8] == str(label)
    }
    assert matched == {(entry.resname, "297")}


# ---------------------------------------------------------------------------
# Divergence case 2: a gap in the author sequence
# ---------------------------------------------------------------------------

def test_author_gap_does_not_gap_the_label_numbering():
    """1IGT chain B jumps author 97 -> 100H; label_seq_id does not jump."""
    _cif, mapping = _convert(IGG_FV)
    b = mapping.polymer_entries("B")
    at_97 = next(e for e in b if (e.author.seq, e.author.icode) == (97, ""))
    after = b[b.index(at_97) + 1]

    assert (after.author.seq, after.author.icode) == (100, "H")
    assert after.label_seq_id == at_97.label_seq_id + 1

    # Author 98 and 99 are absent from the file, so they are refusals.
    for missing in (98, 99):
        with pytest.raises(NumberingError):
            mapping.label_for("B", missing)


def test_a_residue_after_the_gap_is_offset_by_more_than_the_chain_start():
    """The offset is not a constant, so no single arithmetic shift can stand in.

    This is the reason the mapping has to be a table. Chain B's offset before
    the gap differs from its offset after it.
    """
    _cif, mapping = _convert(IGG_FV)
    before_off = 52 - mapping.label_for("B", 52)[1]
    after_off = 101 - mapping.label_for("B", 101)[1]
    assert before_off != after_off


# ---------------------------------------------------------------------------
# Divergence case 3: insertion codes
# ---------------------------------------------------------------------------

def test_insertion_coded_residues_are_separately_addressable():
    """B52 and B52A are two residues with two label_seq_ids, one apart."""
    _cif, mapping = _convert(IGG_FV)
    plain = mapping.label_for("B", 52)
    inserted = mapping.label_for("B", 52, "A")

    assert plain != inserted
    assert inserted[1] == plain[1] + 1
    assert mapping.author_for(*inserted) == AuthorResidue("B", 52, "A")
    # An author id with no insertion code never resolves to the inserted one.
    assert mapping.author_for(*plain) == AuthorResidue("B", 52, "")


def test_a_run_of_insertion_codes_stays_in_file_order():
    """82, 82A, 82B, 82C occupy four consecutive label_seq_ids in that order."""
    _cif, mapping = _convert(IGG_FV)
    labels = [
        mapping.label_for("B", 82, ic)[1] for ic in ("", "A", "B", "C")
    ]
    assert labels == list(range(labels[0], labels[0] + 4))
    assert [mapping.author_for("B", n).icode for n in labels] == ["", "A", "B", "C"]


def test_insertion_codes_shift_every_later_residue_in_that_chain():
    """Insertion codes push every later label_seq_id in that chain further down.

    Chain A of the same file has no insertions, which is what makes the
    comparison meaningful: the same arithmetic does not hold on both chains.
    The exact count is asserted below rather than stated here.
    """
    _cif, mapping = _convert(IGG_FV)
    # Author 45 is label 1. The insertion-coded residues at or below author
    # 100 are 52A, 82A, 82B, 82C and the 100H..100K run; the assert below is
    # what fixes the count, not this comment.
    b_entries = mapping.polymer_entries("B")
    inserted_before_101 = [
        e for e in b_entries if e.author.icode and e.author.seq <= 100
    ]
    assert len(inserted_before_101) == 8  # 52A, 82A/B/C, 100H/I/J/K

    label_101 = mapping.label_for("B", 101)[1]
    plain_before = len([
        e for e in b_entries
        if not e.author.icode and e.author.seq < 101
    ])
    assert label_101 == plain_before + len(inserted_before_101) + 1


# ---------------------------------------------------------------------------
# Divergence case 4: two protomers under one author numbering
# (the shape of the recorded proteina hotspot defect)
# ---------------------------------------------------------------------------

def test_the_same_author_resseq_in_two_chains_maps_to_two_distinct_residues():
    """B52A and D52A are different residues and must not collapse.

    This is the recorded proteina defect's shape: a bare residue id matched one
    protomer of a homodimer and the run proceeded against the wrong one. Here
    the mapping is keyed on the chain, so there is no bare form to get wrong.
    """
    _cif, mapping = _convert(IGG_FV)
    b = mapping.label_for("B", 52, "A")
    d = mapping.label_for("D", 52, "A")

    assert b[0] == "B" and d[0] == "D"
    assert b != d
    assert mapping.author_for(*b).chain == "B"
    assert mapping.author_for(*d).chain == "D"
    # The two protomers happen to share a label_seq_id, which is exactly why the
    # chain half of the key is load-bearing: dropping it would make the two
    # indistinguishable.
    assert b[1] == d[1]


def test_the_fc_homodimer_protomers_differ_in_their_own_offsets():
    """3AVE chain A starts at 234 and chain B at 237, so one offset serves neither.

    A pipeline that computed "label = author - 233" from chain A and applied it
    to chain B would address a residue three positions away.
    """
    _cif, mapping = _convert(FC_DIMER)
    assert mapping.polymer_entries("A")[0].author.seq == 234
    assert mapping.polymer_entries("B")[0].author.seq == 237
    assert mapping.label_for("A", 300)[1] != mapping.label_for("B", 300)[1]
    assert mapping.author_for("B", mapping.label_for("B", 300)[1]) == (
        AuthorResidue("B", 300, "")
    )


def test_a_hotspot_on_a_chain_the_structure_lacks_is_refused():
    """Refused here rather than carried into the container.

    How RFD4 itself treats an id it cannot resolve is UNVERIFIED (scope §10A).
    The precedent for the bad case is proteina's upstream, which drops an
    unmatched token and runs anyway (``tools/proteina/run_pipeline.py:706-715``);
    refusing before the GPU is touched does not depend on which way RFD4 goes.
    """
    _cif, mapping = _convert(FC_DIMER)
    with pytest.raises(NumberingError, match="not in this structure"):
        mapping.label_for("Z", 297)


# ---------------------------------------------------------------------------
# The identity-mapping check: would these tests notice if the mapping were a lie?
# ---------------------------------------------------------------------------

class _IdentityMapping(ResidueMapping):
    """A deliberately wrong mapping: ``label_seq_id`` = author resSeq.

    This is the bug being guarded against, written down. It is the mapping a
    pipeline has implicitly when it passes author numbers straight through.
    """

    def label_for(self, chain, seq, icode=""):
        return chain, int(seq)

    def author_for(self, label_asym_id, label_seq_id):
        return AuthorResidue(label_asym_id, int(label_seq_id), "")


@pytest.mark.parametrize("fixture", [FC_DIMER, IGG_FV], ids=["fc_dimer", "igg_fv"])
def test_identity_mapping_would_fail_the_diverging_fixtures(fixture):
    """On both diverging fixtures the identity mapping gives a DIFFERENT answer.

    A test suite that passed under an identity mapping would prove nothing about
    the real one. This is the check that it does not.
    """
    _cif, real = _convert(fixture)
    identity = _IdentityMapping(real.entries)

    picked = [e for e in real.polymer_entries() if e.label_seq_id != e.author.seq]
    assert picked, "fixture does not diverge; it cannot witness this"

    entry = picked[0]
    real_answer = real.label_for(entry.author.chain, entry.author.seq, entry.author.icode)
    identity_answer = identity.label_for(
        entry.author.chain, entry.author.seq, entry.author.icode
    )
    assert real_answer != identity_answer

    # And the identity mapping's answer names a different residue, or none:
    try:
        collided = real.author_for(*identity_answer)
    except NumberingError:
        collided = None
    assert collided != entry.author


def test_the_easy_fixture_cannot_witness_the_identity_mapping():
    """Lysozyme gives the identity mapping the same answer, by construction.

    Stated as a test so that nobody later "simplifies" the suite down to this
    fixture and believes the coverage survived.
    """
    _cif, real = _convert(LYSOZYME)
    identity = _IdentityMapping(real.entries)
    for entry in real.polymer_entries():
        a = real.label_for(entry.author.chain, entry.author.seq, entry.author.icode)
        b = identity.label_for(entry.author.chain, entry.author.seq, entry.author.icode)
        assert a == b


# ---------------------------------------------------------------------------
# The mapping as a carried value
# ---------------------------------------------------------------------------

def test_mapping_survives_a_json_round_trip():
    _cif, mapping = _convert(IGG_FV)
    restored = ResidueMapping.from_json_obj(json.loads(mapping.to_json()))
    assert restored == mapping
    assert restored.label_for("B", 52, "A") == mapping.label_for("B", 52, "A")


def test_an_unknown_mapping_format_version_is_refused():
    with pytest.raises(NumberingError, match="format_version"):
        ResidueMapping.from_json_obj({"format_version": 99, "residues": []})


def test_a_duplicate_author_residue_is_refused_rather_than_resolved():
    """Two rows claiming the same author identity is an unanswerable question."""
    row = ResidueEntry(
        author=AuthorResidue("A", 1, ""), resname="ALA",
        label_asym_id="A", label_entity_id=1, label_seq_id=1,
    )
    with pytest.raises(NumberingError, match="duplicate author residue"):
        ResidueMapping([row, row])


# ---------------------------------------------------------------------------
# The CIF itself
# ---------------------------------------------------------------------------

def test_the_cif_carries_both_numberings_for_every_atom():
    """auth_seq_id and label_seq_id are both present, and they disagree.

    A reader that resolves against either column therefore has the information
    it needs; a writer that emitted only one would force a guess.
    """
    cif, mapping = _convert(FC_DIMER)
    header = [
        line.strip()[len("_atom_site."):]
        for line in cif.splitlines() if line.startswith("_atom_site.")
    ]
    assert "label_seq_id" in header and "auth_seq_id" in header
    i_label, i_auth = header.index("label_seq_id"), header.index("auth_seq_id")

    rows = [line.split() for line in cif.splitlines() if line.startswith("ATOM")]
    assert rows
    assert any(r[i_label] != r[i_auth] for r in rows)
    # The CIF agrees with the mapping, row by row, for one named residue.
    asym, label = mapping.label_for("A", 297)
    i_asym = header.index("label_asym_id")
    assert {
        r[i_auth] for r in rows if r[i_asym] == asym and r[i_label] == str(label)
    } == {"297"}


def test_the_insertion_code_column_is_written_not_folded_into_the_seq_id():
    cif, _mapping = _convert(IGG_FV)
    header = [
        line.strip()[len("_atom_site."):]
        for line in cif.splitlines() if line.startswith("_atom_site.")
    ]
    i_ic, i_auth, i_asym = (
        header.index("pdbx_PDB_ins_code"),
        header.index("auth_seq_id"),
        header.index("label_asym_id"),
    )
    rows = [line.split() for line in cif.splitlines() if line.startswith("ATOM")]
    inserted = {
        r[i_ic] for r in rows if r[i_asym] == "B" and r[i_auth] == "82"
    }
    assert inserted == {".", "A", "B", "C"}


def test_entity_poly_seq_rows_match_the_polymer_residue_count():
    cif, mapping = _convert(IGG_FV)
    in_loop = False
    rows = 0
    for line in cif.splitlines():
        if line == "_entity_poly_seq.mon_id":
            in_loop = True
            continue
        if in_loop:
            if line.startswith("#"):
                break
            rows += 1
    assert rows == len(mapping.polymer_entries())


def _pdb_line(
    serial: int, name: str, resname: str, chain: str, resseq: str, icode: str,
    z: float = 3.0, element: str = "C", altloc: str = " ",
    record: str = "ATOM  ",
) -> str:
    """One ATOM/HETATM record with every field placed by PDB column.

    ``resseq`` and ``icode`` are strings, not an int, because the tests below
    are about which COLUMNS a number occupies: columns 23-26 are resSeq and
    column 27 is the insertion code, and these tests need to put a digit in
    column 27 without the formatting deciding for them.
    """
    return (
        f"{record:<6s}{serial:>5d} {name:^4s}{altloc}{resname:>3s} {chain}"
        f"{resseq:>4s}{icode}   "
        f"{1.0:8.3f}{2.0:8.3f}{z:8.3f}{1.0:6.2f}{0.0:6.2f}"
        f"{'':10s}{element:>2s}"
    )


def test_a_four_digit_resseq_with_a_digit_insertion_code_is_accepted():
    """``1234`` plus insertion code ``5`` is legal PDB, and was refused.

    The previous revision refused any five consecutive digits across columns
    23-27 as a resSeq overflow. That set contains every legal four-digit
    residue number carrying a DIGIT insertion code -- column 27 is one
    character wide and the format does not restrict it to letters -- so a
    conforming file was rejected outright.
    """
    pdb = (
        _pdb_line(1, "CA", "ALA", "A", "1234", " ") + "\n"
        + _pdb_line(2, "CA", "GLY", "A", "1234", "5", z=4.0) + "\n"
        + "END\n"
    )
    _cif, mapping = _convert_text(pdb)
    keys = [(e.author.seq, e.author.icode, e.label_seq_id) for e in mapping.entries]
    assert keys == [(1234, "", 1), (1234, "5", 2)]


def test_a_low_numbered_resseq_with_a_digit_insertion_code_is_accepted():
    """Continuity without a width condition refuses legal low-numbered files.

    The overflow rule asks whether the resSeq and insertion code READ TOGETHER
    are the previous residue's number plus one. That is only evidence of an
    overflow when the joined reading is five digits wide, because a residue
    numbered under 10000 does not overflow anything. The first revision of the
    rule omitted the width condition, so it fired wherever the arithmetic
    happened to line up at any width: residue 522 followed by a legal resSeq
    ``52`` with insertion code ``3`` joins to 523, one past 522, and was
    refused outright.

    Both a two-digit and a one-digit resSeq in columns 23-26 are exercised
    (``52`` after residue 522, and ``1`` after residue 12), because the defect
    was in the arithmetic rather than at a particular width.
    """
    pdb = (
        _pdb_line(1, "CA", "ALA", "A", "522", " ") + "\n"
        + _pdb_line(2, "CA", "GLY", "A", "52", "3", z=4.0) + "\n"
        + "END\n"
    )
    _cif, mapping = _convert_text(pdb)
    keys = [(e.author.seq, e.author.icode) for e in mapping.entries]
    assert keys == [(522, ""), (52, "3")]

    pdb = (
        _pdb_line(1, "CA", "ALA", "A", "12", " ") + "\n"
        + _pdb_line(2, "CA", "GLY", "A", "1", "3", z=4.0) + "\n"
        + "END\n"
    )
    _cif, mapping = _convert_text(pdb)
    keys = [(e.author.seq, e.author.icode) for e in mapping.entries]
    assert keys == [(12, ""), (1, "3")]


def test_an_overflowed_resseq_after_9999_is_refused():
    """Residue 10000 written after 9999 is caught by continuity, not by shape.

    A residue numbered >= 10000 overflows columns 23-26 into column 27, where
    its first four digits read as the resSeq and its fifth as an insertion
    code. Nothing in the line says which reading was meant: the coordinates
    stay aligned at columns 31-54 either way, so the bytes of a legal
    ``1234`` + ``5`` and of an overflowed ``12345`` are indistinguishable. The
    one discriminator the FILE carries is that the five-digit reading continues
    the chain -- 10000 directly after 9999 -- and that is what is refused here.
    ``pdb_ca_residues`` in `tools/proteina/run_pipeline.py` misparses the same
    input silently, as its own docstring records.
    """
    pdb = (
        _pdb_line(1, "CA", "ALA", "A", "9999", " ") + "\n"
        + _pdb_line(2, "CA", "GLY", "A", "1000", "0", z=4.0) + "\n"
        + "END\n"
    )
    with pytest.raises(NumberingError, match="overflows"):
        pdb_to_cif(pdb)


def test_an_overflowed_resseq_with_no_predecessor_misparses_silently():
    """The ceiling of the continuity rule, pinned as it behaves.

    Continuity needs a predecessor. A chain whose FIRST residue is >= 10000,
    or whose numbering skips the 9999 boundary, offers none, so the same bytes
    are read literally: resSeq 1000, insertion code ``0``. If the file meant
    10000 then that is the wrong residue and nothing fails -- the defect class
    this module exists to prevent, NOT closed here. This test asserts the wrong
    reading deliberately, so that a later fix fails here and has to say so.
    Closing it needs information the line does not carry; the route is to
    require an mmCIF for such a structure, which is a scope decision and not
    this PR's.
    """
    pdb = _pdb_line(1, "CA", "ALA", "A", "1000", "0") + "\nEND\n"
    _cif, mapping = _convert_text(pdb)
    assert [(e.author.seq, e.author.icode) for e in mapping.entries] == [(1000, "0")]


def test_only_the_first_model_is_converted():
    pdb = (
        "MODEL        1\n"
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n"
        "ENDMDL\n"
        "MODEL        2\n"
        "ATOM      2  CA  ALA A   1       9.000   9.000   9.000  1.00  0.00           C\n"
        "ENDMDL\n"
    )
    cif, mapping = _convert_text(pdb)
    assert len(mapping.entries) == 1
    assert len([ln for ln in cif.splitlines() if ln.startswith("ATOM")]) == 1
    assert "1.000" in cif and "9.000" not in cif


def test_a_non_polymer_residue_has_no_label_seq_id_and_no_hotspot():
    """A ligand cannot be given a sequence position, so it is refused, not faked."""
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n"
        "HETATM    2  C1  LIG A 500       4.000   5.000   6.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   2       7.000   8.000   9.000  1.00  0.00           C\n"
    )
    _cif, mapping = _convert_text(pdb)
    lig = next(e for e in mapping.entries if e.resname == "LIG")
    assert lig.label_seq_id is None
    with pytest.raises(NumberingError, match="not a polymer"):
        mapping.label_for("A", 500)
    # The ligand does not consume a sequence position from the polymer either.
    assert mapping.label_for("A", 2) == ("A", 2)


def _convert_text(pdb: str):
    return pdb_to_cif(pdb)


# ---------------------------------------------------------------------------
# Contig range, and the hotspot atom set (§10 item 6 -- undecided)
# ---------------------------------------------------------------------------

def test_label_range_for_chain_is_in_label_numbering_not_author_numbering():
    _cif, mapping = _convert(FC_DIMER)
    lo, hi = label_range_for_chain(mapping, "A")
    assert (lo, hi) == (1, len(mapping.polymer_entries("A")))
    assert hi != mapping.polymer_entries("A")[-1].author.seq


def test_hotspot_atoms_exclude_backbone_and_hydrogens():
    atoms = hotspot_atoms_for_residue(
        "TYR",
        ["N", "CA", "C", "O", "CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH",
         "HB2", "1HB"],
    )
    assert atoms == ("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH")


def test_glycine_hotspot_falls_back_to_CA():
    assert hotspot_atoms_for_residue("GLY", ["N", "CA", "C", "O"]) == ("CA",)


def test_a_backbone_only_residue_with_no_CA_is_refused():
    with pytest.raises(NumberingError):
        hotspot_atoms_for_residue("LYS", ["N", "C", "O"])


def test_a_hotspot_selection_names_the_label_seq_id_not_the_author_resseq():
    """End to end: a picked author residue becomes an upstream selection string."""
    _cif, mapping = _convert(FC_DIMER)
    asym, label = mapping.label_for("A", 297)
    selection = format_hotspot_selection(asym, label, ("CB", "CG"))
    assert selection == f"sel('A/*/{label}/[CB,CG]')"
    assert "297" not in selection


# ---------------------------------------------------------------------------
# Refusals and asym assignment found by review-code, 2026-11-02
# ---------------------------------------------------------------------------

def test_a_repeated_author_residue_is_refused_not_merged():
    """Two residues claiming one author identity must not collapse into one.

    A concatenated file, or a multi-model file missing its ENDMDL, presents the
    same (chain, resSeq, icode) twice with different coordinates. Merging them
    would give one ``label_seq_id`` two sets of atoms and write two CIF rows
    identical in label_asym_id + label_seq_id + label_atom_id, so a hotspot
    would silently name both.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      2  CA  GLY A   2       4.000   5.000   6.000  1.00  0.00           C\n"
        "ATOM      3  CA  ALA A   1      50.000   2.000   3.000  1.00  0.00           C\n"
    )
    with pytest.raises(NumberingError, match="more than once"):
        _convert_text(pdb)


def test_a_blank_chain_id_is_refused():
    """mmCIF has no empty asym id, and a selection with a blank chain field
    addresses nothing, so a blank chain id is refused rather than renamed.

    That tool-generated PDBs leave column 22 blank is UNCITED, here and at the
    refusal itself. The refusal is the tested behaviour either way.
    """
    pdb = (
        "ATOM      1  CA  ALA     1       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      2  CB  ALA     1       1.500   2.500   3.500  1.00  0.00           C\n"
    )
    with pytest.raises(NumberingError, match="blank chain id"):
        _convert_text(pdb)


def test_waters_get_their_own_asym_and_entity():
    """A water carrying the protein's author chain id must not sit inside that
    chain's ``polymer`` entity -- an entity is one chemical entity."""
    _cif, mapping = _convert(LYSOZYME)
    waters = [e for e in mapping.entries if e.resname == "HOH"]
    assert waters, "1HEW carries waters"
    protein = next(e for e in mapping.entries if e.is_polymer)
    assert protein.label_asym_id == "A"
    assert {w.label_asym_id for w in waters} == {"A_HOH"}
    assert all(w.label_entity_id != protein.label_entity_id for w in waters)

    # The CIF agrees: the protein's entity is polymer, the water's is not.
    types = {}
    in_entity = False
    for line in _cif.splitlines():
        if line == "_entity.pdbx_description":
            in_entity = True
            continue
        if in_entity:
            if line.startswith("#"):
                break
            parts = line.split()
            types[int(parts[0])] = parts[1]
    assert types[protein.label_entity_id] == "polymer"
    assert types[waters[0].label_entity_id] == "non-polymer"


def test_old_style_terminal_oxygens_are_backbone():
    """``OT1``/``OT2`` are terminal-oxygen names used in place of ``OXT``
    (which writers still emit them is uncited -- see the comment above
    ``numbering.py``'s ``_BACKBONE``); a C-terminal glycine emitting them must
    still take the CA fallback, not name two oxygens."""
    assert hotspot_atoms_for_residue("GLY", ["N", "CA", "C", "OT1", "OT2"]) == ("CA",)


def test_alternate_conformers_do_not_duplicate_an_atom_name():
    """Each altloc repeats the same heavy-atom names; a selection names each once."""
    atoms = hotspot_atoms_for_residue(
        "SER", ["N", "CA", "C", "O", "CB", "OG", "CB", "OG"]
    )
    assert atoms == ("CB", "OG")


def test_a_resname_change_at_one_author_key_is_refused():
    """Deposited microheterogeneity: two residue types at one author position.

    The repeated-residue guard cannot see this -- the author key never changes --
    so without its own check the two collapse and the CIF gets two rows
    identical in label_asym_id + label_seq_id + label_atom_id for N and CA.
    """
    pdb = (
        "ATOM      1  N   SER A  50       1.000   2.000   3.000  1.00  0.00           N\n"
        "ATOM      2  CA  SER A  50       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      3  OG  SER A  50       1.000   2.000   3.000  1.00  0.00           O\n"
        "ATOM      4  N   ALA A  50       1.000   2.000   3.000  1.00  0.00           N\n"
        "ATOM      5  CA  ALA A  50       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      6  CB  ALA A  50       1.000   2.000   3.000  1.00  0.00           C\n"
    )
    with pytest.raises(NumberingError, match="two residue names"):
        _convert_text(pdb)


def test_a_nonpolymer_sharing_a_polymer_author_number_is_refused():
    """A water numbered to continue the chain must not inherit the chain's asym.

    Classifying the pair by the first atom's resname alone would hand the water
    a ``label_seq_id`` inside the polymer entity -- the merge the non-polymer
    asym split exists to remove, arriving by a different route.
    """
    pdb = (
        "ATOM      1  CA  LEU A 129       1.000   2.000   3.000  1.00  0.00           C\n"
        "HETATM    2  O   HOH A 129       4.000   5.000   6.000  1.00  0.00           O\n"
    )
    with pytest.raises(NumberingError, match="two residue names"):
        _convert_text(pdb)


def test_a_duplicate_label_seq_id_is_refused():
    """``author_for`` is read back into customer-facing ids, so a label claimed
    twice must refuse rather than let the second entry win silently.

    ``pdb_to_cif`` assigns labels monotonically and cannot produce this;
    ``from_json_obj`` on a payload written elsewhere can.
    """
    def entry(seq):
        return ResidueEntry(
            author=AuthorResidue("A", seq, ""),
            resname="ALA",
            label_asym_id="A",
            label_entity_id=1,
            label_seq_id=1,
        )

    with pytest.raises(NumberingError, match="duplicate label_seq_id"):
        ResidueMapping([entry(5), entry(6)])


# ---------------------------------------------------------------------------
# What counts as a chain residue: the record column, not the name
# (review-code, round 3, 2026-11-02)
# ---------------------------------------------------------------------------

def test_an_atom_record_residue_outside_the_name_set_keeps_its_position():
    """An ``ATOM``-record residue whose name this module does not know still
    occupies a sequence position, so the residues after it are not shifted.

    ``UNK`` is the name this test uses. How often it reaches us is uncited;
    the defect does not depend on the frequency. Classifying on the name alone
    skipped it here while ``pdb_ca_residues`` in
    ``tools/proteina/run_pipeline.py`` counted it, so every later
    ``label_seq_id`` in the chain was one too low and a hotspot addressed the
    residue before the one asked for, with nothing failing.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      2  CA  UNK A   2       4.000   5.000   6.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       7.000   8.000   9.000  1.00  0.00           C\n"
    )
    _cif, mapping = _convert_text(pdb)
    assert mapping.label_for("A", 2) == ("A", 2)
    assert mapping.author_for("A", 2) == AuthorResidue("A", 2, "")
    # The residue after it keeps the position it would have had.
    assert mapping.label_for("A", 3) == ("A", 3)
    assert [e.resname for e in mapping.polymer_entries("A")] == ["ALA", "UNK", "GLY"]


def test_a_hetatm_record_standard_residue_is_not_a_chain_residue():
    """A standard amino-acid name on a ``HETATM`` record is a ligand, not a
    chain residue: it takes no sequence position and cannot be addressed.

    The same rule read the other way. ``pdb_ca_residues`` in
    ``tools/proteina/run_pipeline.py`` skips it; classifying on the name alone
    gave it a ``label_seq_id`` here and pushed every later residue in the chain
    one too high.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n"
        "HETATM    2  CA  ALA A   2       4.000   5.000   6.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       7.000   8.000   9.000  1.00  0.00           C\n"
    )
    _cif, mapping = _convert_text(pdb)
    with pytest.raises(NumberingError, match="not a polymer residue"):
        mapping.label_for("A", 2)
    assert mapping.label_for("A", 3) == ("A", 2)
    assert mapping.author_for("A", 2) == AuthorResidue("A", 3, "")


def test_an_atom_record_water_is_not_a_chain_residue():
    """Water is the one residue the record column does not get to promote.

    An ``ATOM``-record water would otherwise be handed a sequence position
    inside the polymer entity -- the merge the non-polymer asym split exists to
    remove. ``_is_polymer`` has no residue-name test at all; what excludes this
    water is that it carries no ``CA``, which is one of the three conditions
    copied from ``pdb_ca_residues``. The record column does not override that,
    which is what this test pins.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      2  O   HOH A   2       4.000   5.000   6.000  1.00  0.00           O\n"
        "ATOM      3  CA  GLY A   3       7.000   8.000   9.000  1.00  0.00           C\n"
    )
    _cif, mapping = _convert_text(pdb)
    with pytest.raises(NumberingError, match="not a polymer residue"):
        mapping.label_for("A", 2)
    assert mapping.label_for("A", 3) == ("A", 2)


def test_a_record_type_change_at_one_author_key_is_refused():
    """One author key whose atoms disagree about ATOM vs HETATM must refuse.

    The record column decides whether the residue takes a sequence position, so
    letting the first atom's record stand for the rest is the same absorbed-field
    bug as taking the first atom's residue name. The name is the same here, so
    the resname guard cannot see it.
    """
    pdb = (
        "ATOM      1  N   ALA A   7       1.000   2.000   3.000  1.00  0.00           N\n"
        "HETATM    2  CA  ALA A   7       4.000   5.000   6.000  1.00  0.00           C\n"
    )
    with pytest.raises(NumberingError, match="mixes ATOM and HETATM records"):
        _convert_text(pdb)


# ---------------------------------------------------------------------------
# Label identity, rebuilt (2026-11-03)
#
# Rounds 3, 4 and 5 each found a defect in how label identity was decided, and
# round 4's was inside round 3's repair for round 3's. The cause was that
# "which residues get a label position" and "which rows share a label identity"
# were each re-guessed from scratch. The two tests below are the definitions,
# not illustrations of them: the first makes disagreement with the in-repo rule
# a test failure, the second makes a duplicate label identity one.
# ---------------------------------------------------------------------------

def _polymer_author_keys(path: Path) -> set[tuple[str, int, str]]:
    """The residues this module gives a ``label_seq_id``, as author keys."""
    _cif, mapping = _convert(path)
    return {
        (e.author.chain, e.author.seq, e.author.icode)
        for e in mapping.entries
        if e.is_polymer
    }


# Inputs chosen so the corpus witnesses the two directions the earlier rounds
# got wrong, plus the GROMACS water spelling that is absent from
# `_WATER_RESNAMES`. Each is a single chain A of three residues.
_CONFORMANCE_CRAFTED = {
    # A calcium ion on an ATOM record. BOTH parsers count it as a chain
    # residue; `test_an_atom_record_ion_is_a_chain_residue_in_both_parsers`
    # measures that and says why it is pinned rather than fixed. It is in this
    # corpus so that excluding the name in `_is_polymer` alone fails
    # `test_polymer_eligibility_matches_pdb_ca_residues` instead of passing it,
    # which is the loophole that test's own docstring warns about.
    "atom_record_ion_with_a_ca_atom": (
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  CA  A   2       1.000   0.000   0.000  1.00  0.00          CA\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "atom_record_nonstandard_with_ca": (
        "ATOM      1  CA  UNK A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "atom_record_cap_without_ca": (
        "ATOM      1  CH3 ACE A   0       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  ALA A   1       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   2       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "hetatm_record_standard_resname": (
        "HETATM    1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "hetatm_record_modres": (
        "HETATM    1  CA  MSE A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "atom_record_water": (
        "ATOM      1  O   HOH A   1       0.000   0.000   0.000  1.00  0.00           O\n"
        "ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "atom_record_gromacs_water": (
        "ATOM      1  OW  SOL A   1       0.000   0.000   0.000  1.00  0.00           O\n"
        "ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "atom_record_ion_without_ca": (
        "ATOM      1 ZN    ZN A   1       0.000   0.000   0.000  1.00  0.00          ZN\n"
        "ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
    "side_chain_only_standard_resname": (
        "ATOM      1  CB  LEU A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00  0.00           C\n"
    ),
}


def test_polymer_eligibility_matches_pdb_ca_residues(tmp_path):
    """The two implementations of "is this a chain residue" must agree here.

    ``numbering.py`` cannot import ``pdb_ca_residues``: the rfd4 container
    mounts one tools-side file, which is why ``_MODRES_EQUIV`` is copied there
    rather than imported. A copy drifts, and the drift is silent -- a residue
    one parser counts and the other skips shifts every later ``label_seq_id`` in
    that chain, so a hotspot addresses its neighbour and the run completes. This
    test is therefore where the two are held together: it runs both
    implementations over the corpus below and fails on any disagreement. That is
    a corpus, not a proof -- three deposited structures and the crafted cases,
    chosen because each earlier round's defect was a case no test covered. A
    disagreement on an input unlike all of them would still pass.
    """
    from tools.proteina.run_pipeline import pdb_ca_residues

    corpus = [LYSOZYME, FC_DIMER, IGG_FV]
    for name, text in _CONFORMANCE_CRAFTED.items():
        path = tmp_path / f"{name}.pdb"
        path.write_text(text + "END\n")
        corpus.append(path)

    disagreements = {}
    for path in corpus:
        reference, n_unparsable = pdb_ca_residues(path)
        assert n_unparsable == 0, f"{path.name}: corpus must parse cleanly"
        ours = _polymer_author_keys(path)
        if ours != set(reference):
            disagreements[path.name] = {
                "ours_only": sorted(ours - set(reference)),
                "reference_only": sorted(set(reference) - ours),
            }
    assert not disagreements, f"polymer eligibility diverged: {disagreements}"


def _atom_site_rows(cif: str) -> tuple[list[str], list[list[str]]]:
    """The ``_atom_site`` column names and data rows of a generated CIF."""
    cols: list[str] = []
    rows: list[list[str]] = []
    for line in cif.splitlines():
        stripped = line.strip()
        if stripped.startswith("_atom_site."):
            cols.append(stripped.split(".", 1)[1])
        elif cols and stripped.startswith(("ATOM", "HETATM")):
            rows.append(stripped.split())
        elif cols and rows:
            break
    return cols, rows


def _duplicate_non_water_label_keys(path: Path) -> dict:
    """Label identities that more than one atom row shares, waters excluded.

    Water is excluded because a chain's waters deliberately share one asym and
    are told apart only by ``auth_seq_id``
    (``test_waters_get_their_own_asym_and_entity``); every other non-polymer
    gets one ``label_asym_id`` per copy, so a shared identity there is a defect.
    The attribution of that split to RCSB is labelled UNVERIFIED in
    ``pdb_to_cif``'s docstring; this helper does not depend on it.

    The identity keyed on is ``label_asym_id`` + ``label_seq_id`` +
    ``label_atom_id`` + ``label_alt_id``. ``label_comp_id`` is read only to
    skip waters and is not part of the key. ``label_alt_id`` IS part of it, so
    two alternate locations of one atom are not reported as a duplicate.
    """
    cif, _mapping = _convert(path)
    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    counts: dict[tuple[str, str, str, str], int] = {}
    for row in rows:
        if row[ix["label_comp_id"]].upper() in _WATER_RESNAMES:
            continue
        key = (
            row[ix["label_asym_id"]], row[ix["label_seq_id"]],
            row[ix["label_atom_id"]], row[ix["label_alt_id"]],
        )
        counts[key] = counts.get(key, 0) + 1
    return {k: v for k, v in counts.items() if v > 1}


def _load_generator():
    """Import `scripts/generate_rfd4_atom_elements.py` by path.

    `scripts/` is not a package, and that script's work sits behind
    ``if __name__ == "__main__"``, so importing it runs nothing and fetches
    nothing.
    """
    import importlib.util

    path = REPO / "scripts" / "generate_rfd4_atom_elements.py"
    spec = importlib.util.spec_from_file_location("_gen_rfd4_atoms", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_200_that_parses_to_no_atoms_raises():
    """``fetch_atoms`` returns None for an absent component and nothing else.

    The ion loop in that script SKIPS a ``None``, so anything that reads as
    absence quietly writes a smaller ion table and pushes those residues back
    onto the shape fallback the script exists to retire -- while the run still
    prints success. A 200 whose body this parser cannot read is not absence, and
    an ``or None`` tail used to report it as absence anyway.

    No network: ``urlopen`` is replaced with a stub, which is also what makes
    this a test of the error path rather than of the dictionary. A fresh module
    object is loaded here, so the replacement cannot leak into another test.
    """
    import contextlib
    import io
    import urllib.error

    gen = _load_generator()

    @contextlib.contextmanager
    def _serves(body: str):
        yield io.BytesIO(body.encode())

    gen.urllib.request.urlopen = lambda *a, **k: _serves("<html>nope</html>")
    with pytest.raises(RuntimeError, match="parse to no atoms"):
        gen.fetch_atoms("ZN")

    # An empty body is the same case, not a special one.
    gen.urllib.request.urlopen = lambda *a, **k: _serves("")
    with pytest.raises(RuntimeError, match="parse to no atoms"):
        gen.fetch_atoms("ZN")

    # A real 404 is still the one thing that means absence.
    def _404(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", None, None)

    gen.urllib.request.urlopen = _404
    assert gen.fetch_atoms("ZZZ") is None

    # Anything else propagates.
    def _503(*a, **k):
        raise urllib.error.HTTPError("u", 503, "Unavailable", None, None)

    gen.urllib.request.urlopen = _503
    with pytest.raises(urllib.error.HTTPError):
        gen.fetch_atoms("ZN")

    # And a well-formed monatomic body still parses, so the guard above is not
    # simply refusing everything.
    gen.urllib.request.urlopen = lambda *a, **k: _serves(
        "_chem_comp_atom.comp_id ZN\n"
        "_chem_comp_atom.atom_id ZN\n"
        "_chem_comp_atom.type_symbol ZN\n"
    )
    assert gen.fetch_atoms("ZN") == [("ZN", "Zn")]


def test_two_ligand_copies_an_insertion_code_apart_get_distinct_asyms():
    """A ligand asym id must be injective on the author residue key.

    The non-polymer asym id is built from the author chain, the residue name
    and the author position. With the sequence number and the insertion code
    CONCATENATED, author ``1`` with insertion code ``1`` and author ``11`` with
    none both render ``A_NAG_11``, so two distinct ligand copies land in one
    asym. A ligand's ``label_seq_id`` is ``.``, so the two then share
    ``label_asym_id`` + ``label_seq_id`` + ``label_atom_id`` +
    ``label_alt_id`` -- the duplicate label identity
    ``test_each_ligand_copy_gets_its_own_asym`` exists to exclude, reached by a
    route the three deposited fixtures do not carry because none of them gives
    a ligand an insertion code.

    Nothing failed on it either. ``ResidueMapping.__init__`` checks ``by_label``
    for duplicates only where ``label_seq_id`` is not None, which no
    non-polymer has, so the collision was silent in both the file and the
    mapping.
    """
    pdb = (
        _pdb_line(1, "CA", "ALA", "A", "5", " ") + "\n"
        + _pdb_line(2, "C1", "NAG", "A", "1", "1", z=4.0, record="HETATM") + "\n"
        + _pdb_line(3, "C1", "NAG", "A", "11", " ", z=5.0, record="HETATM") + "\n"
        + "END\n"
    )
    cif, mapping = _convert_text(pdb)

    asyms = [
        e.label_asym_id for e in mapping.entries
        if e.author.seq in (1, 11) and e.resname == "NAG"
    ]
    assert len(asyms) == 2
    assert asyms[0] != asyms[1]

    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    keys = [
        (row[ix["label_asym_id"]], row[ix["label_seq_id"]],
         row[ix["label_atom_id"]], row[ix["label_alt_id"]])
        for row in rows if row[ix["label_comp_id"]] == "NAG"
    ]
    assert len(keys) == 2
    assert len(set(keys)) == 2


@pytest.mark.parametrize("fixture", [LYSOZYME, FC_DIMER, IGG_FV])
def test_each_ligand_copy_gets_its_own_asym(fixture):
    """No two atom rows may share a label identity, water aside.

    Non-polymer ``label_seq_id`` is ``.``, so grouping non-polymers by
    ``(chain, resname)`` made N copies of one ligand indistinguishable in label
    numbering: same asym, same ``.``, same ``label_comp_id``, same
    ``label_atom_id``. Any reader that groups atom rows by that identity --
    which is what the ``label_*`` columns are for in mmCIF -- then sees one
    ligand carrying N copies of each atom; whether upstream RFD4's reader groups
    that way is UNVERIFIED. Measured against this file as it stood at commit
    ``a4108d6d`` by
    substituting it and re-running this test, these fixtures produced 14 such
    keys (1HEW) and 50 (3ave, whose chain C is an N-glycan with four NAGs); 1igt_fv
    carries no non-polymer records and produced 0, which is why it cannot
    witness the defect on its own.
    """
    assert _duplicate_non_water_label_keys(fixture) == {}


def test_a_two_letter_element_survives_a_blank_element_column():
    """Columns 77-78 are optional; a one-letter guess invented a non-element.

    ``ZN`` was written ``type_symbol Z``, which is not an element, and
    selenomethionine's ``SE`` was written ``S``. Nothing failed -- the CIF
    stayed well formed and the numbering was untouched -- which is why only an
    assertion on the value catches it.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00\n"
        "HETATM    2 SE   MSE A   2       4.000   5.000   6.000  1.00  0.00\n"
        "HETATM    3 ZN    ZN B   1       7.000   8.000   9.000  1.00  0.00\n"
    )
    cif, _mapping = _convert_text(pdb)
    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    by_name = {r[ix["label_atom_id"]]: r[ix["type_symbol"]] for r in rows}
    assert by_name == {"CA": "C", "SE": "Se", "ZN": "Zn"}


def test_a_hydrogen_name_in_column_13_is_not_mercury():
    """``HG12`` starts in column 13 like a two-letter element does.

    The two-letter fallback is therefore gated on an element list, not on the
    column alone -- ``HG`` is mercury and ``HG12`` is a valine hydrogen, and
    they are indistinguishable by position.
    """
    pdb = (
        "ATOM      1  CA  VAL A   1       1.000   2.000   3.000  1.00  0.00\n"
        "ATOM      2 HG12 VAL A   1       1.500   2.500   3.500  1.00  0.00\n"
    )
    cif, _mapping = _convert_text(pdb)
    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    assert {r[ix["label_atom_id"]]: r[ix["type_symbol"]] for r in rows} == {
        "CA": "C", "HG12": "H",
    }


def test_deuterium_is_not_a_side_chain_heavy_atom():
    """A deuterated structure spells its hydrogens ``D``, not ``H``."""
    names = ["N", "CA", "C", "O", "CB", "CG1", "CG2", "DB", "DG11", "HG12"]
    assert hotspot_atoms_for_residue("VAL", names) == ("CB", "CG1", "CG2")


def test_diverges_is_true_for_an_insertion_coded_chain_at_the_same_seq():
    """An insertion-coded residue diverges even where the seq ids coincide.

    ``7`` and ``7A`` are two residues at one author number, so the second gets
    ``label_seq_id`` 2 while its author resSeq is still 7. Neither residue's
    ids coincide -- the first is ``label_seq_id`` 1 against author 7 -- so the
    assertions name both residues that make the predicate true rather than
    asserting the predicate alone.
    """
    pdb = (
        "ATOM      1  CA  ALA A   7       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      2  CA  GLY A   7A      4.000   5.000   6.000  1.00  0.00           C\n"
    )
    _cif, mapping = _convert_text(pdb)
    assert mapping.label_for("A", 7, "") == ("A", 1)
    assert mapping.label_for("A", 7, "A") == ("A", 2)
    assert mapping.diverges() is True


def test_a_space_insertion_code_is_the_same_residue_as_no_insertion_code():
    """``" "`` from PDB column 27 must address the residue with no icode.

    ``" "`` is truthy, so the old ``icode or ""`` left it as ``" "`` and the
    lookup refused a residue that is present. ``_parse_atoms`` strips the
    column, so this only bit a caller handing column 27 over raw -- which is
    what staging code reading the PDB itself would do.
    """
    pdb = (
        "ATOM      1  CA  ALA A   7       1.000   2.000   3.000  1.00  0.00           C\n"
        "ATOM      2  CA  GLY A   8       4.000   5.000   6.000  1.00  0.00           C\n"
    )
    _cif, mapping = _convert_text(pdb)
    assert mapping.label_for("A", 7, " ") == mapping.label_for("A", 7, "")
    assert mapping.label_for("A", 7, " ") == ("A", 1)
def test_a_numbered_two_letter_element_is_not_truncated_to_one():
    """``MG1`` was written ``type_symbol M``, and M is not an element.

    The first repair of the blank-element-column defect admitted a two-letter
    element only when the atom name was EXACTLY the symbol, so every numbered
    variant -- ``FE1`` in an Fe-S cluster, ``CL1`` on a halogenated ligand,
    ``MG1`` -- fell back to its first letter: iron written as fluorine, chlorine
    as carbon, magnesium as a non-element. Three characters in columns 13-15,
    while a three-character hydrogen name is right-justified into column 14, so
    one trailing digit separates them; ``HG12`` is four characters and stays
    excluded, which the mercury test pins.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00\n"
        "HETATM    2 FE1  FES B   1       4.000   5.000   6.000  1.00  0.00\n"
        "HETATM    3 CL1  LIG B   2       7.000   8.000   9.000  1.00  0.00\n"
        "HETATM    4 MG1   MG B   3      10.000  11.000  12.000  1.00  0.00\n"
    )
    cif, _mapping = _convert_text(pdb)
    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    assert {r[ix["label_atom_id"]]: r[ix["type_symbol"]] for r in rows} == {
        "CA": "C", "FE1": "Fe", "CL1": "Cl", "MG1": "Mg",
    }


def test_a_structure_with_no_polymer_residue_is_refused():
    """A ligand-only upload wrote an `_entity_poly_seq` loop with no rows.

    Every loop carrying sequence information draws its rows from the polymer
    entries, so with none the writer emitted the tags and then the terminating
    `#`, which CIF 1.1 grammar does not allow -- and which gemmi nonetheless
    accepts, so a lenient reader would not have reported it. Refused instead of
    emitted empty: an input with no polymer residue has no chain for a contig to
    cover and no residue id for a hotspot to name.
    """
    pdb = (
        "HETATM    1 C1   NAG A   1       1.000   2.000   3.000  1.00  0.00\n"
        "HETATM    2  O   HOH A   2       4.000   5.000   6.000  1.00  0.00\n"
    )
    with pytest.raises(NumberingError, match="no polymer residue"):
        _convert_text(pdb)
def test_common_side_chain_names_are_not_metals():
    """``CD1`` became cadmium, ``HG2`` mercury: the whole side chain went metal.

    The previous revision read the atom name alone, admitting any column-13
    two-letter element prefix with one trailing digit. For a writer that
    left-justifies every name into column 13 and omits columns 77-78 that
    covers the most common side-chain atoms in any protein -- ``CD1``/``CD2``
    (leucine), ``CE1``-``CE3`` (phenylalanine, tryptophan), ``ND1``/``ND2``
    (histidine, asparagine) and ``HG2``/``HG3`` (methionine) -- because ``CD``,
    ``CE``, ``ND`` and ``HG`` are all real elements. Nothing failed: every
    ``type_symbol`` was a well-formed element symbol, just the wrong one.

    The residue name is the discriminator, and after round 9 it is looked up
    rather than reasoned about: these four residues are in
    ``_CCD_COVERED_RESIDUES``, so no shape test runs on their atom names at
    all. That cadmium is never deposited inside a leucine is not cited here
    and is not what makes this pass.
    """
    pdb = (
        "ATOM      1  CA  LEU A   1       1.000   2.000   3.000  1.00  0.00\n"
        "ATOM      2 CD1  LEU A   1       1.000   2.000   4.000  1.00  0.00\n"
        "ATOM      3 CD2  LEU A   1       1.000   2.000   5.000  1.00  0.00\n"
        "ATOM      4  CA  PHE A   2       2.000   2.000   3.000  1.00  0.00\n"
        "ATOM      5 CE1  PHE A   2       2.000   2.000   4.000  1.00  0.00\n"
        "ATOM      6  CA  HIS A   3       3.000   2.000   3.000  1.00  0.00\n"
        "ATOM      7 ND1  HIS A   3       3.000   2.000   4.000  1.00  0.00\n"
        "ATOM      8  CA  MET A   4       4.000   2.000   3.000  1.00  0.00\n"
        "ATOM      9 HG2  MET A   4       4.000   2.000   4.000  1.00  0.00\n"
    )
    cif, _mapping = _convert_text(pdb)
    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    got = {r[ix["label_atom_id"]]: r[ix["type_symbol"]] for r in rows}
    assert got == {
        "CA": "C", "CD1": "C", "CD2": "C", "CE1": "C", "ND1": "N", "HG2": "H",
    }


def test_element_fallback_ceilings():
    """The two cases the element LOOKUP does not reach, pinned as they behave.

    Neither CCD table closes either ceiling: both residues here are in neither
    of them. ``FES`` is a four-atom cluster (``Fe2 S2``) and ``LIG`` a 29-atom
    organic component (``C15 H11 N3``) -- both are real CCD entries, neither is
    monatomic, and neither is a residue this module treats as polymer, so both
    atoms fall through to the shape rule that every earlier version of
    `_element_of` used. What the lookup did close is every residue the
    generator covers, which is where the oscillating defects were; the two
    ceilings were always confined to ligands and to modified residues outside
    ``_MODRES_EQUIV``, and they still are.

    The monatomic-ion rule carries any copy number, so a conforming
    four-character ``MG10`` in residue ``MG`` does resolve. The ceilings are a
    four-character name in a residue that is NOT a monatomic component
    (``FE12`` inside an Fe-S cluster falls to ``F``, the pre-fix defect class),
    and a residue whose atom name begins with two letters that spell an element
    (a ligand carbon named ``CD1`` is written ``Cd``). Lifting the shape rule's
    one-digit limit would close the first and open the second wider -- a
    ligand's ``HG12`` would become mercury -- which is the trade that produced
    two wrong repairs in a row; the route out is to cover the residue in
    `scripts/generate_rfd4_atom_elements.py`, not to refine the guess.

    Both need a blank element column AND a left-justifying writer. This test
    asserts the WRONG values deliberately, so that a later fix fails here and
    has to say so.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00\n"
        "HETATM    2 MG10  MG B   1       4.000   5.000   6.000  1.00  0.00\n"
        "HETATM    3 FE12 FES B   2       7.000   8.000   9.000  1.00  0.00\n"
        "HETATM    4 CD1  LIG B   3      10.000  11.000  12.000  1.00  0.00\n"
    )
    cif, _mapping = _convert_text(pdb)
    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    got = {r[ix["label_atom_id"]]: r[ix["type_symbol"]] for r in rows}
    assert got == {
        "CA": "C",
        "MG10": "Mg",   # rule 3 carries the copy number
        "FE12": "F",    # CEILING: four characters, resname is not the element
        "CD1": "Cd",    # CEILING: non-standard residue, left-justified name
    }


def test_other_terminal_oxygen_spellings_are_backbone():
    """``OC1``/``OC2`` and ``O1``/``O2`` were returned as a glycine side chain.

    ``_BACKBONE`` listed `OXT` and `OT1`/`OT2` but not these, so a C-terminal
    glycine written by a tool that uses them had its two carboxyl oxygens
    selected as "sidechain_heavy" instead of taking the CA fallback. Nothing
    failed: a hotspot was still produced, naming backbone oxygens as the
    contact atoms a binder should be steered at.
    """
    for spellings in (("OC1", "OC2"), ("O1", "O2")):
        assert hotspot_atoms_for_residue(
            "GLY", ["N", "CA", "C", *spellings]
        ) == ("CA",), spellings
def test_selenomethionine_epsilon_carbon_is_not_cerium():
    """``CE`` in an ``MSE`` was written ``Ce``: cerium, in a polymer residue.

    The previous revision read a column-13 two-letter element prefix for any
    residue outside the 20 standard amino acids -- which includes every name in
    ``_MODRES_EQUIV``, the modified residues this module treats AS polymer. So
    selenomethionine had its epsilon carbon turned into a lanthanide while its
    ``SE`` was right by accident. How common MSE is in the PDB is not cited
    here; that it is in ``_MODRES_EQUIV``, and so carried as polymer, is what
    makes this a defect in the mapping's own residues
    (`numbering.py` ``_is_polymer``). The CCD gives both: ``MSE.CE`` is ``C`` and ``MSE.SE`` is ``Se``,
    and `_CCD_ELEMENT_EXCEPTIONS` carries the second because it is the one that
    the name's first letter gets wrong.
    """
    pdb = (
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00\n"
        "HETATM    2  CA  MSE A   2       4.000   5.000   5.000  1.00  0.00\n"
        "HETATM    3 CE   MSE A   2       4.000   5.000   6.000  1.00  0.00\n"
        "HETATM    4 SE   MSE A   2       4.000   5.000   7.000  1.00  0.00\n"
        "HETATM    5 CG   MSE A   2       4.000   5.000   8.000  1.00  0.00\n"
    )
    cif, _mapping = _convert_text(pdb)
    cols, rows = _atom_site_rows(cif)
    ix = {c: i for i, c in enumerate(cols)}
    got = {
        (r[ix["label_comp_id"]], r[ix["label_atom_id"]]): r[ix["type_symbol"]]
        for r in rows
    }
    assert got == {
        ("ALA", "CA"): "C",
        ("MSE", "CA"): "C",
        ("MSE", "CE"): "C",
        ("MSE", "SE"): "Se",
        ("MSE", "CG"): "C",
    }


def test_every_polymer_residue_name_has_a_ccd_element_table():
    """A name added to ``_MODRES_EQUIV`` alone would be guessed, not looked up.

    ``_MODRES_EQUIV`` decides what counts as polymer; ``_CCD_COVERED_RESIDUES``
    decides whose atoms get their element from the dictionary. A residue in the
    first but not the second falls through to the shape rule, which is exactly
    the `MSE` ``CE`` -> cerium defect
    (`test_selenomethionine_epsilon_carbon_is_not_cerium`). This fails if the
    two drift, which they do silently: adding a modified residue to
    ``_MODRES_EQUIV`` without re-running
    `scripts/generate_rfd4_atom_elements.py` changes no behaviour that any
    other test here observes.
    """
    missing = sorted(set(_MODRES_EQUIV) - _CCD_COVERED_RESIDUES)
    assert missing == [], (
        "add these to MODRES in scripts/generate_rfd4_atom_elements.py and "
        f"re-run it: {missing}"
    )
def test_an_atom_record_ion_is_a_chain_residue_in_both_parsers(tmp_path):
    """A calcium ion named ``CA`` enters the sequence. Both parsers admit it.

    A monatomic calcium is deposited as residue ``CA`` whose single atom is
    also named ``CA``. On an ``ATOM`` record `_is_polymer` sees an atom named
    ``CA`` and returns true, so the ion takes a position in
    ``_entity_poly_seq`` and in the contig. It is not a chain residue, and
    every residue after it in that chain is shifted by one.

    This is NOT fixed here, because the flaw is SHARED. ``pdb_ca_residues`` in
    `tools/proteina/run_pipeline.py` applies the same three conditions and
    admits the same ion -- measured below: both return all three residues of
    this fixture. Excluding the name in `_is_polymer` alone would make the two
    parsers disagree, and a disagreement shifts ``label_seq_id`` in exactly the
    way this module exists to prevent. So both behaviours are pinned here, and
    the input is in `_CONFORMANCE_CRAFTED`, which means a one-sided change
    fails this test and `test_polymer_eligibility_matches_pdb_ca_residues`
    together instead of passing both.

    Fixing it for real means changing both parsers in one commit, which reaches
    outside this PR's files. It is reported as an open item, not as done.
    """
    from tools.proteina.run_pipeline import pdb_ca_residues

    path = tmp_path / "ion.pdb"
    path.write_text(_CONFORMANCE_CRAFTED["atom_record_ion_with_a_ca_atom"] + "END\n")

    assert _is_polymer("ATOM  ", "CA", True) is True

    reference, n_unparsable = pdb_ca_residues(path)
    assert n_unparsable == 0
    assert reference == [("A", 1, ""), ("A", 2, ""), ("A", 3, "")]

    _cif, mapping = _convert(path)
    assert [
        (e.resname, e.label_seq_id) for e in mapping.entries
    ] == [("ALA", 1), ("CA", 2), ("GLY", 3)]


def _cif_tokens(line: str) -> list[str]:
    """Split one CIF data line into values, honouring quoted strings.

    `_atom_site_rows` splits on whitespace, which is enough for the rows every
    other test here reads. It cannot read a value that CONTAINS whitespace, and
    the quoting round-trip is what the next test is about. The whole rule: a
    quoted string ends at its own delimiter followed by whitespace or
    end-of-line, and CIF has no escape character, so a delimiter anywhere else
    is ordinary content.
    """
    out: list[str] = []
    i = 0
    while i < len(line):
        if line[i].isspace():
            i += 1
        elif line[i] in "'" + '"':
            quote = line[i]
            j = i + 1
            while j < len(line) and not (
                line[j] == quote
                and (j + 1 == len(line) or line[j + 1].isspace())
            ):
                j += 1
            assert j < len(line), "unterminated quote in " + repr(line)
            out.append(line[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < len(line) and not line[j].isspace():
                j += 1
            out.append(line[i:j])
            i = j
    return out


def test_an_apostrophe_round_trips_without_a_backslash():
    """An apostrophe in a value came back with a backslash welded to it.

    `_cif_value` quoted with ``'`` and put a backslash before every inner
    ``'``. CIF has no escape mechanism, so that backslash is not an escape --
    it is a character added to the value. An apostrophe in the altLoc column,
    which is one character wide and accepts any character, therefore read back
    as two. Nothing failed: the row keeps its column count, so the CIF is well
    formed and one cell is wrong.
    """
    pdb = _pdb_line(1, "CA", "ALA", "A", "1", " ", altloc="'") + "\nEND\n"
    cif, _mapping = _convert_text(pdb)
    cols = [
        ln.strip().split(".", 1)[1]
        for ln in cif.splitlines() if ln.strip().startswith("_atom_site.")
    ]
    row = next(
        _cif_tokens(ln) for ln in cif.splitlines() if ln.strip().startswith("ATOM")
    )
    assert len(row) == len(cols)
    assert row[cols.index("label_alt_id")] == "'"


def test_a_value_that_cannot_be_quoted_is_refused():
    """Neither delimiter can hold it, so it is refused rather than mangled.

    An apostrophe only ends a single-quoted string when whitespace follows it,
    so most apostrophes need no escape and no second delimiter: ``O'Brien x``
    is written between apostrophes unchanged. One followed by whitespace does
    end the string, and there is the double quote to fall back on. A value
    carrying BOTH quote characters, each followed by whitespace or by the end
    of the value, closes whichever delimiter is chosen, and the remaining CIF
    form is a semicolon-delimited text field, which cannot sit inside a loop
    row. That case is refused, and refusing is the alternative to emitting a
    cell that reads back as several.

    An earlier version of this docstring called that refusal unreachable from a
    real file, on the grounds that every cell comes from a fixed-width PDB
    column. That is wrong, and the last three lines of this test are why: the
    residue-name column is three characters wide and ``" '`` fits in it, so
    `pdb_to_cif` refuses a whole file over one malformed column. It is a
    reachable path on a malformed file, not a dead branch.
    """
    assert _cif_value("plain") == "plain"
    assert _cif_value("a b") == "'a b'"
    assert _cif_value("O'Brien x") == "'" + "O'Brien x" + "'"
    assert _cif_value("a' b") == '"' + "a' b" + '"'
    with pytest.raises(NumberingError, match="mmCIF string"):
        _cif_value("a' b" + '" c')

    pdb = _pdb_line(1, "CA", '" \'', "A", "1", " ") + "\nEND\n"
    assert pdb[17:20] == '" \''
    with pytest.raises(NumberingError, match="mmCIF string"):
        pdb_to_cif(pdb)
