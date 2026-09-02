"""Glycosylation sequon detection guards for Epitope Scout.

scout/glycan.py shipped with no test coverage at all, which is how MSE and SEC
came to be dropped from the residue list that detect_glycosylation_sequons
indexes POSITIONALLY at i, i+1, i+2. A dropped in-polymer residue does not
leave a hole there -- it CLOSES the gap and welds two sequence-distant residues
adjacent, so the detector both misses real sequons and invents ones that do not
exist. Same root cause as the pydssp backbone and the phi/psi peptide in
scout/scoring.py; this is the only site in that family that moves a SCORE
rather than a label, since sequons feed score_glycan_proximity and glycan_risk
carries 0.15 of the composite (scout/feasibility.py).

Every arm below is a MET/CYS control pair. MSE is chemically MET and SEC is
chemically CYS, so the control must agree exactly; a disagreement is the bug.

The repo fixtures are HOLLOW for this defect when SeMet-ized wholesale -- 1HEW
has no sequons at all, and both Fc fixtures carry their MET too far from N297
to matter, so all three pass on the broken code. The arms here therefore place
the modified residue at a position that provably discriminates, and
_assert_discriminating pins that: if a fixture ever stops being able to fail,
the test says so instead of quietly passing.
"""

import io
from pathlib import Path

import pytest
from Bio.PDB import PDBParser

from scout.glycan import _THREE_TO_ONE, detect_glycosylation_sequons

# The test names the two residues itself rather than importing the module
# constant. Depending on scout.glycan._MODIFIED_AA here would make the whole
# file fail to COLLECT against the pre-fix code, which is a much weaker proof
# than watching the assertions below go red.
_MODIFIED = frozenset({"MSE", "SEC"})

_EXAMPLES = Path(__file__).resolve().parents[1] / "static" / "example"
_FC = _EXAMPLES / "3ave_igg1_fc_dimer.pdb"

# Real IgG1 Fc N-glycosylation site: ASN297-SER298-THR299. SER298 is the x
# position, so a modified residue there is exactly the "welds the sequon shut"
# case -- and N297 is the single most consequential sequon in antibody work.
_SEQUON_ASN, _X_POS = 297, 298

# ASN286-ALA287-LYS288-THR289 is NOT a sequon (x=A, third residue K).
# Dropping ALA287 closes the gap to N286-LYS288-THR289, a fabricated "N-K-T".
_FAKE_ASN, _FAKE_DROP = 286, 287


def _pre_fix_detect(chain):
    """Verbatim pre-fix logic (scout/glycan.py at 3b3802b).

    Kept in the test rather than described in prose so every arm can assert it
    is still capable of failing.
    """
    canon = {k: v for k, v in _THREE_TO_ONE.items() if k not in _MODIFIED}
    std = [r for r in chain.get_residues() if r.id[0] == " " and r.resname in canon]
    found = set()
    for i in range(len(std) - 2):
        aa = [canon.get(std[i + k].resname, "?") for k in (0, 1, 2)]
        if aa[0] == "N" and aa[1] != "P" and aa[2] in ("S", "T"):
            found.add((std[i].id[1], "-".join(aa)))
    return found


def _substitute(text, resnum, resname, record="HETATM", chain="A"):
    """Rewrite one residue of a PDB, leaving every coordinate untouched."""
    out = []
    for line in text.splitlines(True):
        if (line.startswith(("ATOM  ", "HETATM"))
                and line[21] == chain
                and line[22:26].strip() == str(resnum)):
            line = record.ljust(6) + line[6:17] + resname.ljust(3) + line[20:]
        out.append(line)
    return "".join(out)


def _inject_het_residue(text, before_resnum, resname, new_resnum=900, chain="A"):
    """Splice a het-recorded residue into the MIDDLE of the polymer.

    Rewriting an existing residue cannot test this: the point is a residue the
    polymer does not contain. It is cloned from the coordinates of
    ``before_resnum`` so the atoms are real, re-recorded as HETATM under a
    CANONICAL resname, and inserted directly ahead of that residue -- which is
    where a positional window can actually see it.
    """
    lines, out, donor = text.splitlines(True), [], []
    for line in lines:
        if (line.startswith(("ATOM  ", "HETATM"))
                and line[21] == chain
                and line[22:26].strip() == str(before_resnum)):
            donor.append("HETATM" + line[6:17] + resname.ljust(3) + line[20:22]
                         + str(new_resnum).rjust(4) + line[26:])
    assert donor, f"no donor atoms at residue {before_resnum}"
    for line in lines:
        if (donor and line.startswith(("ATOM  ", "HETATM"))
                and line[21] == chain
                and line[22:26].strip() == str(before_resnum)):
            out.extend(donor)
            donor = []
        out.append(line)
    return "".join(out)


def _model(text):
    return PDBParser(QUIET=True).get_structure("x", io.StringIO(text))[0]


def _sequons(text, chain="A"):
    found = detect_glycosylation_sequons(_model(text)[chain])
    return {(d["resnum"], d["motif"]) for d in found}


def _assert_discriminating(text, expected, chain="A"):
    """The fixture must be able to FAIL on the pre-fix code, or the arm proves
    nothing. Without this, a later fixture edit turns the guard hollow in
    silence -- it would still pass, just against a detector that cannot be
    wrong here any more.
    """
    stale = _pre_fix_detect(_model(text)[chain])
    assert stale != expected, (
        "fixture no longer discriminates: the pre-fix detector already agrees "
        f"with the control ({stale}), so this arm would pass on the broken code"
    )


@pytest.fixture(scope="module")
def fc_text():
    return _FC.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. A modified residue at the x position must not erase a real sequon.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("record", ["HETATM", "ATOM"])
@pytest.mark.parametrize(
    "modified,control",
    [("MSE", "MET"), ("SEC", "CYS")],
    ids=["selenomethionine", "selenocysteine"],
)
def test_modified_residue_at_x_keeps_the_sequon(fc_text, modified, control, record):
    """N297-x-T299 survives when x is MSE/SEC, exactly as when x is MET/CYS.

    ``record`` covers both spellings: real depositions write HETATM, while
    design and refinement pipelines often re-emit the same residue as ATOM.
    The fix matches on resname alone, so the two must behave identically.
    """
    want = _sequons(_substitute(fc_text, _X_POS, control, "ATOM  "))
    assert (_SEQUON_ASN, f"N-{_THREE_TO_ONE[control]}-T") in want, (
        "control lost the sequon, so the fixture is not testing what it claims"
    )

    text = _substitute(fc_text, _X_POS, modified, record)
    _assert_discriminating(text, want)
    assert _sequons(text) == want


# ---------------------------------------------------------------------------
# 2. A modified residue must not close a gap and invent a sequon.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "modified,control",
    [("MSE", "MET"), ("SEC", "CYS")],
    ids=["selenomethionine", "selenocysteine"],
)
def test_modified_residue_does_not_fabricate_a_sequon(fc_text, modified, control):
    """N286-A287-K288-T289 is not a sequon and must not become one.

    Dropping A287 welds N286 onto K288-T289 and reports a glycan site that does
    not exist. That is the direction which silently DEPRESSES glycan_risk, and
    the more common of the two: 64 fabricated against 40 missed over 1542
    MSE-bearing chains from 597 real SeMet depositions.
    """
    want = _sequons(_substitute(fc_text, _FAKE_DROP, control, "ATOM  "))
    assert not any(num == _FAKE_ASN for num, _ in want), (
        "control already fabricates, so the fixture cannot show the defect"
    )

    text = _substitute(fc_text, _FAKE_DROP, modified)
    _assert_discriminating(text, want)
    assert _sequons(text) == want
    assert not any(num == _FAKE_ASN for num, _ in _sequons(text))


# ---------------------------------------------------------------------------
# 3. Cross-module agreement on the one-letter spelling.
# ---------------------------------------------------------------------------

def test_modified_residue_letters_match_epitope_db():
    """scout/epitope_db.py maps the same two residues, and the motif string is
    user-visible, so the two modules must not drift apart on the spelling.
    """
    from scout.epitope_db import _THREE_TO_ONE as DB_MAP
    from scout.glycan import _MODIFIED_AA

    assert _MODIFIED_AA == _MODIFIED, "glycan changed which residues it treats as modified"
    for resname in _MODIFIED:
        assert _THREE_TO_ONE[resname] == DB_MAP[resname], resname


def test_modified_residues_never_read_as_asn_ser_thr_or_pro():
    """Whatever letter they carry, MSE/SEC must not themselves look like a
    sequon position -- only like a legal x.
    """
    for resname in _MODIFIED:
        assert _THREE_TO_ONE[resname] not in ("N", "S", "T", "P")


# ---------------------------------------------------------------------------
# 4. The hetflag gate still holds for everything that is NOT MSE/SEC.
# ---------------------------------------------------------------------------

def test_a_ligand_cannot_enter_the_polymer_sequence(fc_text):
    """The relaxation is a WHITELIST, not "any HETATM".

    There is no peptide-bond continuity test in this function, so a ligand
    admitted to the list would weld the residues either side of it together --
    the same defect pointing the other way, and this time it FABRICATES at
    N297, the site the whole fixture exists for.

    The obvious version of this test is hollow. 3ave carries only ZN and HOH,
    neither of which is in _THREE_TO_ONE, so dropping the hetflag gate admits
    nothing; and every one of its heteroatoms sits after the last polymer ATOM
    line, where no positional window can reach it. Both facts are asserted
    below so the arm cannot quietly revert to that weaker form.
    """
    chain = _model(fc_text)["A"]
    het = [r for r in chain.get_residues() if r.id[0] != " "]
    assert {r.resname.strip() for r in het} == {"ZN", "HOH"}, (
        "fixture heteroatoms changed; re-check that this arm still needs the splice"
    )
    assert not {r.resname.strip() for r in het} & set(_THREE_TO_ONE), (
        "a fixture heteroatom is now a canonical resname, which would make the "
        "no-op assertion below meaningful on its own -- rewrite this arm"
    )

    # GLY is canonical, so only the hetflag can keep it out, and it lands
    # between N297 and S298: admitting it reports N-G-S at the same resnum.
    spiked = _sequons(_inject_het_residue(fc_text, _X_POS, "GLY"))
    assert (_SEQUON_ASN, "N-G-S") not in spiked, (
        "a het-recorded ligand entered the polymer and fabricated a sequon"
    )
    assert spiked == _sequons(fc_text)

def test_proline_at_x_still_blocks_the_sequon(fc_text):
    """Pins the predicate the fix reaches through but does not own.

    N-P-S/T is not glycosylated, and this file is the only coverage
    scout/glycan.py has, so removing that clause would otherwise go unnoticed:
    every other arm here holds x fixed at a non-proline residue.
    """
    assert (_SEQUON_ASN, "N-S-T") in _sequons(fc_text), "fixture lost N297"
    blocked = _sequons(_substitute(fc_text, _X_POS, "PRO", "ATOM  "))
    assert not any(num == _SEQUON_ASN for num, _ in blocked)


# ---------------------------------------------------------------------------
# 5. Inert on structures that carry no modified residue.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name", ["1HEW.pdb", "3ave_igg1_fc_dimer.pdb", "3s7g_fc_ab.pdb"]
)
def test_unmodified_fixtures_are_untouched(name):
    """What the fix must NOT do.

    These carry no MSE/SEC, so the pre-fix and fixed detectors have to agree
    exactly. That is what makes the arms above attributable to the modified
    residues rather than to the rewrite.
    """
    text = (_EXAMPLES / name).read_text(encoding="utf-8", errors="replace")
    for chain in _model(text):
        assert not {r.resname.strip() for r in chain.get_residues()} & _MODIFIED
        got = {(d["resnum"], d["motif"]) for d in detect_glycosylation_sequons(chain)}
        assert got == _pre_fix_detect(chain)


def _pdb_line(serial, atom_name, resname, chain, resnum, x, hetatm=False):
    """One PDB ATOM/HETATM record at exact column positions."""
    line = list(" " * 80)

    def put(text, start):  # 1-indexed PDB columns
        for offset, char in enumerate(str(text)):
            line[start - 1 + offset] = char

    put("HETATM" if hetatm else "ATOM  ", 1)
    put(f"{serial:5d}", 7)
    put(f" {atom_name:<3s}", 13)
    put(f"{resname:>3s}", 18)
    put(chain, 22)
    put(f"{resnum:4d}", 23)
    put(f"{x:8.3f}", 31)
    put(f"{0.0:8.3f}", 39)
    put(f"{0.0:8.3f}", 47)
    put("  1.00 20.00", 55)
    put("C", 77)
    return "".join(line).rstrip()


def test_a_selenomethionine_altloc_twin_does_not_hide_a_sequon():
    """ASN-[MET/MSE at one resnum]-THR is a real N-x-S/T sequon.

    Partial Se incorporation deposits MET and MSE at the SAME residue number as
    two altlocs whose hetflags differ (" " vs "H_MSE"), so Biopython yields two
    Residue objects and both satisfy the residue filter. The filtered list is
    then indexed POSITIONALLY at i, i+1, i+2, so the twin shifts the window:
    the chain reads N-M-M-T and no sequon is reported.

    Admitting MSE without deduplicating made this case WORSE than the hetflag
    gate it replaced, which dropped the HETATM twin and found the sequon. That
    is a regression in the direction the fix exists to prevent, so it is pinned
    here rather than left to the sibling modules' guards.
    """
    import io

    from Bio.PDB import PDBParser

    lines, serial = [], 1
    for resname, resnum, x, hetatm in (
        ("ASN", 1, 3.8, False),
        ("MET", 2, 7.6, False),   # the ATOM half of the altloc pair
        ("MSE", 2, 7.6, True),    # the HETATM half, same residue number
        ("THR", 3, 11.4, False),
    ):
        for atom_name in ("N", "CA", "C", "O", "CB"):
            lines.append(
                _pdb_line(serial, atom_name, resname, "A", resnum, x, hetatm)
            )
            serial += 1

    structure = PDBParser(QUIET=True).get_structure(
        "twin", io.StringIO("\n".join(lines) + "\nEND\n")
    )
    found = detect_glycosylation_sequons(structure[0]["A"])

    assert [(f["resnum"], f["motif"]) for f in found] == [(1, "N-M-T")], (
        f"the MET/MSE altloc twin hid a real N-M-T sequon: {found}"
    )


def test_a_free_ligand_cannot_evict_a_real_residue_at_its_position():
    """A blank-hetflag residue wins a position collision, whatever the file order.

    The dedupe added for the altloc twin originally kept whichever residue came
    first in the file. A free MSE/SEC ligand whose (resseq, icode) collides with
    a real residue then evicted it when its HETATM records were written first --
    legal PDB, and the whole sequon is lost. That is the same optimistic
    direction as the bug the dedupe exists to fix.

    scout/polymer.py cannot hit this because it dedupes AFTER a connectivity
    test, so a free ligand is never a candidate. This module has no coordinates
    for such a test, so it resolves the collision by hetflag instead.
    """
    import io

    from Bio.PDB import PDBParser

    ligand = (
        "HETATM  900  N   MSE A   1      50.000   0.000   0.000  1.00 0.00           N\n"
        "HETATM  901  CA  MSE A   1      51.400   0.000   0.000  1.00 0.00           C\n"
    )
    body = (
        "ATOM      1  N   ASN A   1       0.000   0.000   0.000  1.00 0.00           N\n"
        "ATOM      2  CA  ASN A   1       1.458   0.000   0.000  1.00 0.00           C\n"
        "ATOM      3  C   ASN A   1       2.009   1.420   0.000  1.00 0.00           C\n"
        "ATOM      4  N   LYS A   2       2.530   2.850   0.000  1.00 0.00           N\n"
        "ATOM      5  CA  LYS A   2       3.988   2.850   0.000  1.00 0.00           C\n"
        "ATOM      6  C   LYS A   2       4.539   4.270   0.000  1.00 0.00           C\n"
        "ATOM      7  N   THR A   3       5.060   5.700   0.000  1.00 0.00           N\n"
        "ATOM      8  CA  THR A   3       6.518   5.700   0.000  1.00 0.00           C\n"
        "ATOM      9  C   THR A   3       7.069   7.120   0.000  1.00 0.00           C\n"
    )

    for label, text in (("hetatm first", ligand + body), ("atom first", body + ligand)):
        chain = PDBParser(QUIET=True).get_structure(
            label, io.StringIO(text + "END\n")
        )[0]["A"]
        found = detect_glycosylation_sequons(chain)
        assert [(f["resnum"], f["motif"]) for f in found] == [(1, "N-K-T")], (
            f"with records ordered {label}, a free MSE ligand at resseq 1 "
            f"displaced the real ASN and the sequon was lost: {found}"
        )


def _residue_lines(resname, resnum, x, hetatm=False, start=1):
    """PDB records for one residue at exact column positions."""
    out, serial = [], start
    for atom_name in ("N", "CA", "C", "O", "CB"):
        line = list(" " * 80)

        def put(text, col, line=line):
            for offset, char in enumerate(str(text)):
                line[col - 1 + offset] = char

        put("HETATM" if hetatm else "ATOM  ", 1)
        put("%5d" % serial, 7)
        put(" %-3s" % atom_name, 13)
        put("%3s" % resname, 18)
        put("A", 22)
        put("%4d" % resnum, 23)
        put("%8.3f" % x, 31)
        put("%8.3f" % 0.0, 39)
        put("%8.3f" % 0.0, 47)
        put("  1.00 20.00", 55)
        put("C", 77)
        out.append("".join(line).rstrip())
        serial += 1
    return out


def _chain_from(rows):
    import io

    from Bio.PDB import PDBParser

    lines, serial = [], 1
    for resname, resnum, x, hetatm in rows:
        block = _residue_lines(resname, resnum, x, hetatm, serial)
        lines.extend(block)
        serial += len(block)
    return PDBParser(QUIET=True).get_structure(
        "s", io.StringIO("\n".join(lines) + "\nEND\n")
    )[0]["A"]


def test_a_distant_residue_sharing_a_number_does_not_evict_an_in_polymer_mse():
    """Only an ADJACENT same-position residue is an altloc twin.

    Resolving a (resseq, icode) collision by hetflag alone, without asking
    whether the two are the same residue, let any blank-hetflag residue in the
    chain evict a genuine in-polymer MSE -- including one 200 A away in a
    duplicate-numbered segment, which fusion constructs and renumbered uploads
    produce. The real sequon was then deleted, biasing glycan_risk optimistic:
    the exact direction this module's fix exists to correct.
    """
    chain = _chain_from([
        ("ALA", 9, 0.0, False),
        ("ASN", 10, 3.8, False),
        ("MSE", 11, 7.6, True),      # the real x of the sequon
        ("THR", 12, 11.4, False),
        ("VAL", 13, 15.2, False),
        ("PRO", 11, 200.0, False),   # unrelated, merely shares the number
    ])
    found = [(f["resnum"], f["motif"]) for f in detect_glycosylation_sequons(chain)]
    assert found == [(10, "N-M-T")], (
        "a distant PRO sharing residue number 11 evicted the in-polymer MSE "
        "and deleted a real sequon: %r" % (found,)
    )


def test_a_free_ligand_does_not_hoist_a_real_residue_into_its_slot():
    """The survivor of a collision keeps its OWN position in the list.

    Recording the slot on first sight fixed it by whichever residue LOST, so a
    free MSE written at the head of the file hoisted a real ASN out of its own
    window -- destroying the true N-S-T and FABRICATING an N-A-S. A fabricated
    sequon fabricates the user-facing warning with it.
    """
    chain = _chain_from([
        ("MSE", 11, 300.0, True),    # free ligand, written first, shares a number
        ("ALA", 10, 0.0, False),
        ("ASN", 11, 3.8, False),
        ("SER", 12, 7.6, False),
        ("THR", 13, 11.4, False),
        ("VAL", 14, 15.2, False),
    ])
    found = [(f["resnum"], f["motif"]) for f in detect_glycosylation_sequons(chain)]
    assert found == [(11, "N-S-T")], (
        "the real N-S-T was lost or a sequon fabricated by slot inheritance: %r"
        % (found,)
    )
