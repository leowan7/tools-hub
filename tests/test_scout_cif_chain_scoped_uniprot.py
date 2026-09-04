"""The mmCIF UniProt lookup must answer for the chain it was asked about.

``_extract_uniprot_from_cif`` used to end with a fallback: if no
``_struct_ref_seq`` row named the requested chain, return the first UNP
accession anywhere in the file. That is the chain-scoping defect
``tests/test_scout_chain_scoped_results.py`` exists for, one layer down --
and it was not confined to chains that do not exist.

Measured against real depositions before the fix:

    5YTL.cif chain Z (absent)      -> A0A1W6VP04
    7K8M.cif chains A, B (present) -> P0DTC2
    1IGT.cif chain A   (present)   -> P01863

7K8M A and B are the Fab. Their ``_struct_ref`` rows carry ``db_name PDB``,
so they never enter the accession map, and both were answered with the
SARS-CoV-2 spike -- the ANTIGEN's accession returned for the ANTIBODY, in the
one structure class this tool is built for. 1IGT chain A, the light chain, was
answered with the heavy chain's.

The DBREF-parsing branch has always been chain-scoped, so the two formats
disagreed on identical input: the .cif and the .pdb of one structure gave
different answers for the same chain. Be precise about which function that is
-- the dispatcher the two branches share, ``_extract_uniprot_from_dbref``, was
NOT chain-scoped for a .cif, because it routed straight into the leaky path.
``test_the_wrapper_agrees`` below pins exactly that.

Why the 70% identity gate in ``resolve_uniprot_id`` is not the guard: step 1
runs with ``must_validate=False``, and the identity branch needs BOTH a
UniProt sequence and an extractable chain sequence. Lose either one -- an
unreachable API, or a chain with no standard residues -- and the wrong
accession is accepted with ``identity: None`` and goes on to key the
known-binder lookup.

Hermetic: fixtures are written to tmp_path, nothing touches the network.

    pytest tests/test_scout_cif_chain_scoped_uniprot.py -v
"""

from __future__ import annotations

import pytest

from scout import epitope_db

# Shaped like 7K8M: a Fab whose _struct_ref rows are db_name PDB (no UniProt
# cross-reference at all) plus one UNP-referenced antigen chain. This is the
# arrangement that made the fallback return the antigen for the antibody.
CIF_LOOP = """data_TEST
#
loop_
_struct_ref.id
_struct_ref.db_name
_struct_ref.pdbx_db_accession
1 PDB TEST
2 PDB TEST
3 UNP P0DTC2
#
loop_
_struct_ref_seq.align_id
_struct_ref_seq.ref_id
_struct_ref_seq.pdbx_strand_id
1 1 A
2 2 B
3 3 E
#
"""

# Shaped like 5YTL, the file in the report: one chain, and mmCIF writes
# single-row categories as bare key/value pairs rather than a loop. Note this
# is NOT a separate parse path -- MMCIF2Dict stores every value as a list, so
# a one-row category comes back as ['A'], not 'A', and the isinstance(..., str)
# normalisation in the extractor stays unexercised by this file. What the
# fixture is really for is the ten-character accession, which is where the two
# file formats still legitimately diverge (see PDB_PAIR below).
CIF_SINGLE = """data_TEST
#
_struct_ref.id                  1
_struct_ref.db_name             UNP
_struct_ref.pdbx_db_accession   A0A1W6VP04
#
_struct_ref_seq.align_id        1
_struct_ref_seq.ref_id          1
_struct_ref_seq.pdbx_strand_id  A
#
"""

# A UNP reference with nothing linking it to any chain. The loop over
# _struct_ref_seq never iterates at all here, which is a different traversal
# from "iterates and matches nothing" -- and the pre-fix function answered
# P00698 for it.
CIF_NO_STRUCT_REF_SEQ = """data_TEST
#
_struct_ref.id                  1
_struct_ref.db_name             UNP
_struct_ref.pdbx_db_accession   P00698
#
"""

# One structure in both formats, for the cross-format comparison. The
# accession is 1HEW's six-character P00698 rather than 5YTL's ten-character
# A0A1W6VP04 on purpose, and the reason is worth stating exactly, because
# there are two separate things going on and only one of them is the famous
# eight-column truncation:
#
#   * ``line[33:41]`` reads eight characters, so a ten-character accession
#     truncates to A0A1W6VP and ``_valid_accession`` rejects it. This is live
#     for real files, not just hand-made ones: an AlphaFold DB model of a
#     ten-character A0A... entry puts that accession on a plain ``DBREF``
#     line, so it truncates and is refused. That is what
#     ``resolve_uniprot_id``'s step-2 docstring means when it cites
#     A0A2K5QDT7. Models of shorter accessions are unaffected.
#   * 5YTL's own .pdb does not reach that code at all: RCSB wrote it as
#     ``DBREF1``/``DBREF2``. Until #223 that meant it resolved to nothing,
#     and 5YTL was the standing example of the two formats disagreeing. #223
#     reads the two-line record, so 5YTL now answers A0A1W6VP04 from BOTH
#     formats. Do not reintroduce it as a divergence example.
#
# The truncation is separate from chain scoping and would mask it, so the
# cross-format test below uses a six-character accession on a plain DBREF.
PDB_PAIR = (
    "DBREF  1HEW A    1   129  UNP    P00698   LYC_CHICK"
    "        19     147\n"
    "ATOM      1  CA  ALA A   1       0.000   0.000   0.000"
    "  1.00 20.00           C\nEND\n"
)

# 5YTL's real two-line record, as RCSB emits it. The accession lives on the
# DBREF2 line; DBREF1 carries the database name, so db_name casing on the
# two-line form is decided there and nowhere else.
PDB_TWO_LINE = (
    "DBREF1 5YTL A    2   323  UNP                  A0A1W6VP04_GEOTD\n"
    "DBREF2 5YTL A     A0A1W6VP04                         31         352\n"
    "ATOM      1  CA  ALA A   1       0.000   0.000   0.000"
    "  1.00 20.00           C\nEND\n"
)

CIF_PAIR = """data_TEST
#
_struct_ref.id                  1
_struct_ref.db_name             UNP
_struct_ref.pdbx_db_accession   P00698
#
_struct_ref_seq.align_id        1
_struct_ref_seq.ref_id          1
_struct_ref_seq.pdbx_strand_id  A
#
"""


@pytest.fixture
def loop_cif(tmp_path):
    path = tmp_path / "loop.cif"
    path.write_text(CIF_LOOP, encoding="utf-8")
    return path


@pytest.fixture
def single_cif(tmp_path):
    path = tmp_path / "single.cif"
    path.write_text(CIF_SINGLE, encoding="utf-8")
    return path


class TestAbsentChainsGetNothing:
    """The reported case: a chain that is not in the file."""

    @pytest.mark.parametrize("chain", ["Z", "Q", "", "AA"])
    def test_a_chain_not_in_the_file_resolves_to_empty(self, single_cif, chain):
        assert epitope_db._extract_uniprot_from_cif(str(single_cif), chain) == ""

    def test_a_file_with_no_struct_ref_seq_at_all_resolves_to_empty(
        self, tmp_path
    ):
        """Distinct traversal from the tests above: there the loop iterates
        and matches nothing, here it never iterates. Reachable in a stripped
        or tool-generated .cif. Measured discriminating -- the pre-fix
        function returned P00698 for this input."""
        cif = tmp_path / "no_seq.cif"
        cif.write_text(CIF_NO_STRUCT_REF_SEQ, encoding="utf-8")
        assert epitope_db._extract_uniprot_from_cif(str(cif), "A") == ""

    def test_the_wrapper_agrees(self, single_cif):
        """The fallback lived below ``_valid_accession``, so a well-formed
        wrong accession passed the format check and was returned anyway."""
        assert epitope_db._extract_uniprot_from_dbref(single_cif, "Z") == ""

    def test_both_file_formats_answer_a_bogus_chain_identically(self, tmp_path):
        """A user uploading the .cif and the .pdb of one structure must not
        get two different answers for the same chain. Scoping is what this
        pins; the formats can still diverge for reasons of their own, all of
        them about how wide an accession the PDB record can carry (see above
        PDB_PAIR). 5YTL used to be the example and no longer is -- #223 reads
        its DBREF1/DBREF2 pair, so both formats now give A0A1W6VP04."""
        pdb = tmp_path / "pair.pdb"
        pdb.write_text(PDB_PAIR, encoding="utf-8")
        cif = tmp_path / "pair.cif"
        cif.write_text(CIF_PAIR, encoding="utf-8")

        assert epitope_db._extract_uniprot_from_dbref(pdb, "Z") == ""
        assert epitope_db._extract_uniprot_from_dbref(cif, "Z") == ""

        # ...and still agree where the chain is real, or the assertions above
        # pass for the trivial reason that nothing resolves any more.
        assert (
            epitope_db._extract_uniprot_from_dbref(pdb, "A")
            == epitope_db._extract_uniprot_from_dbref(cif, "A")
            == "P00698"
        )


class TestTheTwoFormatsReadDbNameTheSameWay:
    """Second, smaller divergence in the same pair of functions: the mmCIF
    branch upper-cased ``db_name`` before comparing it and the PDB branch did
    not, so a lowercase ``unp`` resolved from a .cif and not from a .pdb.

    Every file RCSB emits is uppercase, so this is slack rather than a live
    failure -- but it is slack on only one of two branches that are supposed
    to agree, which is how the fallback above went unnoticed too.

    Since #223 the PDB side reads db_name in TWO places -- the plain DBREF
    record and the DBREF1 half of the two-line pair -- so both are covered
    here. Patching only the branch that happened to conflict during the
    rebase would have rebuilt the same one-of-two asymmetry inside the
    format that already had it.
    """

    def test_a_lowercase_db_name_resolves_in_both_formats(self, tmp_path):
        pdb = tmp_path / "lower.pdb"
        # Same length, so the fixed DBREF column positions are undisturbed.
        pdb.write_text(PDB_PAIR.replace("UNP", "unp"), encoding="utf-8")
        cif = tmp_path / "lower.cif"
        cif.write_text(CIF_PAIR.replace("UNP", "unp"), encoding="utf-8")

        assert (
            epitope_db._extract_uniprot_from_dbref(pdb, "A")
            == epitope_db._extract_uniprot_from_dbref(cif, "A")
            == "P00698"
        )

    def test_a_lowercase_db_name_on_the_two_line_record_resolves_too(
        self, tmp_path
    ):
        """The DBREF1 half is the only one naming the database, so it is the
        only place a two-line pair can be refused for its db_name.

        Uses 5YTL's real record. Without .upper() on that second read this
        returns "" while the .cif of the same structure returns the
        accession -- the asymmetry this class exists for, one record type
        over.
        """
        pdb = tmp_path / "lower_two_line.pdb"
        pdb.write_text(PDB_TWO_LINE.replace("UNP", "unp"), encoding="utf-8")
        assert (
            epitope_db._extract_uniprot_from_dbref(pdb, "A") == "A0A1W6VP04"
        )


class TestPresentChainsWithNoUniProtReferenceGetNothingEither:
    """The larger half: chains that DO exist but carry no UNP cross-reference.

    This is where the fallback did real damage, because it is the normal shape
    of an antibody-antigen deposition.
    """

    @pytest.mark.parametrize("antibody_chain", ["A", "B"])
    def test_an_antibody_chain_is_not_given_the_antigens_accession(
        self, loop_cif, antibody_chain
    ):
        assert (
            epitope_db._extract_uniprot_from_cif(str(loop_cif), antibody_chain) == ""
        ), "a db_name PDB chain must not inherit the file's UNP accession"


class TestRealChainsStillResolve:
    """The guard must not cost the feature it protects."""

    def test_the_unp_chain_in_a_loop_still_resolves(self, loop_cif):
        assert epitope_db._extract_uniprot_from_cif(str(loop_cif), "E") == "P0DTC2"

    def test_the_single_value_form_still_resolves(self, single_cif):
        assert (
            epitope_db._extract_uniprot_from_cif(str(single_cif), "A") == "A0A1W6VP04"
        )

    def test_chains_sharing_one_ref_id_each_resolve(self, tmp_path):
        """4HHB's shape: two UniProt entries over four chains, one
        _struct_ref_seq row per chain.

        Be honest about what this does and does not prove. Replaying the
        pre-fix function on the real 4HHB returns the SAME four accessions --
        every chain is named by a row whose ref_id is in the map, so the
        fallback was never reached. A/B/C/D are regression cover only. The
        one key here that discriminates is "Z", which was P69905 before the
        fix and is "" after; do not trim it as noise.
        """
        cif = tmp_path / "hetero.cif"
        cif.write_text(
            "data_TEST\n#\n"
            "loop_\n"
            "_struct_ref.id\n"
            "_struct_ref.db_name\n"
            "_struct_ref.pdbx_db_accession\n"
            "1 UNP P69905\n"
            "2 UNP P68871\n"
            "#\n"
            "loop_\n"
            "_struct_ref_seq.align_id\n"
            "_struct_ref_seq.ref_id\n"
            "_struct_ref_seq.pdbx_strand_id\n"
            "1 1 A\n"
            "2 2 B\n"
            "3 1 C\n"
            "4 2 D\n"
            "#\n",
            encoding="utf-8",
        )
        got = {
            c: epitope_db._extract_uniprot_from_cif(str(cif), c) for c in "ABCDZ"
        }
        assert got == {
            "A": "P69905",
            "B": "P68871",
            "C": "P69905",
            "D": "P68871",
            "Z": "",
        }


class TestTheGuardFailsWhenItIsRemoved:
    """A guard nobody has watched fail is a guess.

    Reinstating the fallback must reproduce the wrong answers, or the
    assertions above pass for some reason other than the one claimed.

    Two honest caveats. First, this test cannot itself go red when the fix is
    reverted -- it patches in its own lenient function either way, so it is a
    demonstration, not a guard. The guards are the tests above: every one of
    them was measured red against the pre-fix module, except those asserting
    that real chains still resolve, which were true either way. Deliberately
    not a count -- the count in this sentence went stale twice while the file
    was being written. Second, ``lenient``
    below is a lookalike, not the deleted code: the original keyed a dict by
    ``_struct_ref.id`` and returned the first value, this zips db_name to
    accession and ignores id. The two agree on both fixtures used here and
    diverge on inputs neither fixture produces (a missing ``_struct_ref.id``,
    a duplicated ref_id), which is why the fixtures are named explicitly.
    """

    def test_reinstating_the_fallback_reintroduces_the_wrong_answer(
        self, monkeypatch, single_cif, loop_cif
    ):
        real = epitope_db._extract_uniprot_from_cif

        def lenient(cif_path, chain_id):
            scoped = real(cif_path, chain_id)
            if scoped:
                return scoped
            # Stands in for the deleted code: first UNP accession in the file.
            from Bio.PDB.MMCIF2Dict import MMCIF2Dict  # noqa: PLC0415

            parsed = MMCIF2Dict(cif_path)
            names = parsed.get("_struct_ref.db_name", [])
            accessions = parsed.get("_struct_ref.pdbx_db_accession", [])
            if isinstance(names, str):
                names, accessions = [names], [accessions]
            for name, accession in zip(names, accessions):
                if name.upper() in ("UNP", "SWS", "TRE"):
                    return accession
            return ""

        monkeypatch.setattr(epitope_db, "_extract_uniprot_from_cif", lenient)

        # Both halves of the defect come back.
        assert (
            epitope_db._extract_uniprot_from_cif(str(single_cif), "Z")
            == "A0A1W6VP04"
        ), "the mutation did not apply -- the assertions above prove nothing"
        assert epitope_db._extract_uniprot_from_cif(str(loop_cif), "A") == "P0DTC2"
