"""Guards against the known-binder lookup answering for the wrong chain.

SAbDab's ``antigen_chain`` column is a pipe-separated list of every chain in
the antigen, sorted alphabetically rather than ranked. ``_parse_summary_csv``
used to keep only ``.split("|")[0]`` and ``_fetch_and_compute_contacts``
measured that one chain, so for a hetero-oligomeric antigen the residues
reported were whichever chain sorts first -- not the chain the user asked
about.

Measured on the live SAbDab summary on 2026-11-06: of 20362 rows that name an
antigen chain, 10316 (50.7%) name more than one. PDB 10EN is the demonstrated
case -- its rows are ``Hchain F, Lchain E, antigen_chain C|D``, where C is
GRIN1 (``DBREF ... UNP Q5R1P0``) and D is GRIN2B
(``DBREF1/DBREF2 ... A0A8B7RCM5``), and ``_rcsb_pdb_ids_for_uniprot`` returns
10EN for the GRIN2B accession. Reading the real file, chain C contacts 18
residues and chain D contacts 1, so a GRIN2B query was shown 18 GRIN1 residue
numbers as its epitope -- numbers which also drive the epitope-overlap badges.

The fixtures here are built in-process from a few ATOM and DBREF records;
nothing reaches the network.
"""

import pytest

from scout import epitope_db

pytest.importorskip("Bio")
pytest.importorskip("numpy")

NEWLINE = chr(10)  # heredoc-safe; literal escapes get mangled in this repo's tooling


def _atom(serial: int, chain: str, resseq: int, x: float, y: float, z: float) -> str:
    """One CA ATOM record of a glycine, columns per the wwPDB format."""
    return (
        "ATOM  {:>5}  CA  GLY {}{:>4}    "
        "{:>8.3f}{:>8.3f}{:>8.3f}  1.00  0.00           C"
    ).format(serial, chain, resseq, x, y, z)


def _dbref(chain: str, first: int, last: int, accession: str) -> str:
    line = "DBREF  1XYZ {}{:>5}{:>6}  UNP    {}".format(chain, first, last, accession)
    # The parser reads the chain at index 12 and the accession from index 33;
    # assert the construction rather than trusting the format string.
    assert line[12] == chain, line
    assert line[26:32] == "UNP   ", repr(line[26:32])
    assert line[33:].split(" ")[0] == accession, repr(line[33:])
    return line


# Chain A: the alphabetically first antigen chain, and NOT the target.
#   Three residues 3 A from antibody chain H -- all three are contacts.
# Chain B: the target. One residue 3.5 A from antibody chain L.
# So the chain that sorts first also has the MOST contacts: picking by either
# rule answers for the wrong protein.
_CHAINS = (
    [_atom(1 + i, "A", 1 + i, 0.0, 4.0 * i, 0.0) for i in range(3)]
    + [_atom(11 + i, "B", 10 + i, 20.0, 4.0 * i, 0.0) for i in range(3)]
    + [_atom(21 + i, "H", 1 + i, 3.0, 4.0 * i, 0.0) for i in range(3)]
    + [_atom(31, "L", 1, 16.5, 0.0, 0.0)]
)

_TARGET = "Q22222"
_OTHER = "P11111"

_PDB_BOTH_TYPED = NEWLINE.join(
    [_dbref("A", 1, 3, _OTHER), _dbref("B", 10, 12, _TARGET)] + _CHAINS + ["END"]
)

# Same coordinates, but chain B carries no UniProt provenance, so nothing ties
# either present chain to the target.
_PDB_TARGET_UNTYPED = NEWLINE.join(
    [_dbref("A", 1, 3, _OTHER)] + _CHAINS + ["END"]
)

# Same coordinates, both antigen chains the target protein (a homodimer
# listed "A|B").
_PDB_HOMODIMER = NEWLINE.join(
    [_dbref("A", 1, 3, _TARGET), _dbref("B", 10, 12, _TARGET)] + _CHAINS + ["END"]
)

# Same coordinates, both antigen chains a protein that is NOT the target.
# RCSB returned the entry for the target, so the target is in the file -- just
# not among the chains SAbDab calls the antigen.
_PDB_FOREIGN = NEWLINE.join(
    [_dbref("A", 1, 3, _OTHER), _dbref("B", 10, 12, _OTHER)] + _CHAINS + ["END"]
)

_AB = ["H", "L"]


def test_fixture_chain_a_is_what_the_old_code_would_have_reported():
    """The wrong answer is reachable in this fixture, not hypothetical.

    Without this, a regression could hide behind a chain A that was never a
    contact in the first place.
    """
    assert epitope_db._compute_contacts(_PDB_BOTH_TYPED, "A", _AB) == [1, 2, 3]
    assert epitope_db._compute_contacts(_PDB_BOTH_TYPED, "B", _AB) == [10]


def test_multi_chain_antigen_reports_the_chain_the_antibody_binds():
    """Chain B's residues, named as chain B, for a query about chain B."""
    out = epitope_db._resolve_antigen_chain(
        _PDB_BOTH_TYPED, ["A", "B"], _AB, _TARGET
    )
    assert out == {"contact_residues": [10], "contact_chain": "B"}


def test_the_other_accession_gets_the_other_chain():
    """The same entry, the same candidates, the other target: chain A."""
    out = epitope_db._resolve_antigen_chain(
        _PDB_BOTH_TYPED, ["A", "B"], _AB, _OTHER
    )
    assert out == {"contact_residues": [1, 2, 3], "contact_chain": "A"}


def test_unresolvable_chain_refuses_instead_of_answering():
    out = epitope_db._resolve_antigen_chain(
        _PDB_TARGET_UNTYPED, ["A", "B"], _AB, _TARGET
    )
    assert out == {"contact_residues": [], "contact_unresolved": ["A", "B"]}
    # A refusal must not look like a measured interface: no contact_chain, and
    # contact_unresolved is what renderKnownBinders branches on so the row is
    # not labelled "no contacts recorded".
    assert "contact_chain" not in out


def test_copies_of_the_target_give_the_copy_the_antibody_touches():
    """A homodimer of the target: both chains qualify, one is measured.

    Chain A has 3 contacts and chain B has 1, so the reported chain is A.
    """
    out = epitope_db._resolve_antigen_chain(
        _PDB_HOMODIMER, ["A", "B"], _AB, _TARGET
    )
    assert out == {"contact_residues": [1, 2, 3], "contact_chain": "A"}


def test_chains_of_another_protein_refuse_rather_than_answer():
    """Positive evidence that neither chain is the target is still a refusal."""
    out = epitope_db._resolve_antigen_chain(
        _PDB_FOREIGN, ["A", "B"], _AB, _TARGET
    )
    assert out == {"contact_residues": [], "contact_unresolved": ["A", "B"]}


def test_single_candidate_needs_no_provenance():
    """The untouched majority path: one antigen chain, answered as before."""
    out = epitope_db._resolve_antigen_chain(
        _PDB_TARGET_UNTYPED, ["B"], _AB, _TARGET
    )
    assert out == {"contact_residues": [10], "contact_chain": "B"}


def test_one_of_several_candidates_surviving_still_needs_provenance():
    """A listed chain absent from the file must not promote the other one.

    Chain Z is not in the structure, so only A is measurable -- but SAbDab
    named two, and A is the other protein. Answering here would be the same
    wrong-chain answer with one candidate instead of two.
    """
    out = epitope_db._resolve_antigen_chain(
        _PDB_BOTH_TYPED, ["A", "Z"], _AB, _TARGET
    )
    assert out == {"contact_residues": [], "contact_unresolved": ["A"]}
    # The chain that IS the target is still answered on the same shape.
    out = epitope_db._resolve_antigen_chain(
        _PDB_BOTH_TYPED, ["B", "Z"], _AB, _TARGET
    )
    assert out == {"contact_residues": [10], "contact_chain": "B"}


def test_absent_candidates_settle_empty_without_a_chain():
    out = epitope_db._resolve_antigen_chain(
        _PDB_BOTH_TYPED, ["Z"], _AB, _TARGET
    )
    assert out == {"contact_residues": [], "contact_chain": ""}


def test_unreadable_coordinates_stay_unsettled():
    """``None`` survives the resolver, so the entry is retried, not cached."""
    assert epitope_db._resolve_antigen_chain("not a pdb", ["A"], _AB, _TARGET) is None


def test_summary_parse_keeps_every_antigen_chain():
    """The plumbing that feeds the resolver: no component is dropped."""
    text = NEWLINE.join([
        "PDB,Hchain,Lchain,antigen_chain,resolution,antigen_species",
        "pdb_000010en,F,E,C | D,3.00,Homo sapiens",
        "pdb_00001abc,H,L,NA | B,2.00,Homo sapiens",
    ])
    index = epitope_db._parse_summary_csv(text)
    assert index["10EN"][0]["antigen_chains"] == ["C", "D"]
    # The literal "NA" is dropped from inside the list, not just when alone.
    assert index["1ABC"][0]["antigen_chains"] == ["B"]
    # The old single-chain key stays, as the display label.
    assert index["10EN"][0]["antigen_chain"] == "C"


def test_resolver_is_given_the_candidates_and_the_target(monkeypatch):
    """_resolve_contacts passes the whole list and the accession down.

    The decision above is only correct if it is reached with the real inputs;
    this pins the one call site that supplies them.
    """
    seen = {}

    def _fake(pdb_id, antigen_chains, ab_chains, target_accession=""):
        seen.update(
            pdb_id=pdb_id, chains=antigen_chains, target=target_accession
        )
        return {"contact_residues": [10], "contact_chain": "B"}

    monkeypatch.setattr(epitope_db, "_fetch_and_compute_contacts", _fake)
    monkeypatch.setattr(epitope_db, "_PDB_FILE_RETRY_AT", 0.0)
    entries = [{
        "pdb_id": "10EN",
        "antigen_chain": "C",
        "antigen_chains": ["C", "D"],
        "ab_chains": ["F", "E"],
    }]
    out = epitope_db._resolve_contacts(_TARGET, entries, 5)
    assert seen == {"pdb_id": "10EN", "chains": ["C", "D"], "target": _TARGET}
    assert out[0]["contact_residues"] == [10]
    assert out[0]["contact_chain"] == "B"
