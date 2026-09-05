"""Tests for shared.uniprot_lookup — DBREF parsing + AlphaFold URL building."""
from __future__ import annotations

from pathlib import Path

import pytest

from shared.uniprot_lookup import (
    alphafold_api_url,
    extract_uniprot_map,
    lookup_uniprot_for_chain,
)


PDB_DIR = Path(__file__).resolve().parents[1] / "tmp" / "pdb_compare"


def test_no_dbref_returns_empty():
    pdb = b"HEADER    NO DBREF\nATOM 1 N ALA A 1 1 1 1 1 1\nEND\n"
    assert extract_uniprot_map(pdb) == {}


def test_dbref_unp_record_parsed():
    pdb = (
        b"HEADER    P25779-style\n"
        b"DBREF  3IUT A    1   215  UNP    P25779   CYSP_TRYCR     123    337\n"
        b"END\n"
    )
    m = extract_uniprot_map(pdb)
    assert "A" in m
    rec = m["A"]
    assert rec.uniprot_accession == "P25779"
    assert rec.chain_id == "A"
    assert rec.pdb_res_begin == 1
    assert rec.pdb_res_end == 215
    assert rec.uniprot_res_begin == 123
    assert rec.uniprot_res_end == 337


def test_dbref_non_unp_skipped():
    """A DBREF pointing at GenBank, not UniProt, must not be returned."""
    pdb = (
        b"HEADER    GB-only\n"
        b"DBREF  1XYZ A    1   215  GB     AAB12345 GBNAME        123    337\n"
        b"END\n"
    )
    assert extract_uniprot_map(pdb) == {}


def test_invalid_accession_skipped():
    """A malformed UniProt accession isn't returned."""
    pdb = (
        b"HEADER    bad accession\n"
        b"DBREF  1XYZ A    1   215  UNP    nopenope CYSP_TRYCR    123    337\n"
        b"END\n"
    )
    assert extract_uniprot_map(pdb) == {}


def test_ten_character_accession_survives_the_field_width():
    """A 10-character accession overflows the 8-wide DBREF field.

    The fixture below is the DBREF record of
        https://alphafold.ebi.ac.uk/files/AF-A0A2K5QDT7-F1-model_v6.pdb
    byte for byte except its trailing pad to column 80. That .pdb records no
    model version at all — the version lives in the URL, and its TITLE
    "V2.0" is the pipeline. The companion .cif names the entry as
    ``_entry.id AF-A0A2K5QDT7-F1``.

    The spec gives the accession columns 34-41 and offers DBREF1/DBREF2 for
    anything longer; this entry ignores that and writes the full accession
    through the field, shifting the entry name right. Reading a fixed
    8-wide slice returned "A0A2K5QD", which is not an accession, so the
    chain resolved as unmapped and the AlphaFold swap was never offered.

    The seq begin/end columns move with it, so they are read from the
    wrong place and are gated off here. Were they not, this line would be
    saved only by luck: its first window raises, short-circuiting a second
    that would have parsed as 1 against a record saying 130.
    """
    pdb = (
        b"HEADER                                            01-JUN-22\n"
        b"DBREF  XXXX A    1   130  UNP    A0A2K5QDT7 A0A2K5QDT7_CEBIM     1    130\n"
        b"END\n"
    )
    m = extract_uniprot_map(pdb)
    assert m["A"].uniprot_accession == "A0A2K5QDT7"
    assert lookup_uniprot_for_chain(pdb, "A") == "A0A2K5QDT7"
    assert m["A"].uniprot_res_begin is None
    assert m["A"].uniprot_res_end is None


def test_overflow_does_not_carry_a_number_from_the_shifted_columns():
    """The seq begin/end windows move with the accession, so they are skipped.

    On the real AlphaFold line both fields come back None whether or not they
    are skipped, because the FIRST window lands in the mnemonic and raises,
    short-circuiting the second in the shared ``try``. That makes the real
    line unable to tell a deliberate skip from luck. Here the entry name is
    short enough that both shifted windows hold digits, so reading them puts
    2 and 99 into a record whose own columns say 42 and 99.

    Widening the accession read is what made this reachable: before it, the
    line was dropped at the format check and never built a record at all.
    """
    pdb = (
        b"HEADER    short entry name\n"
        b"DBREF  1ABC A    1   130  UNP    A0A2K5QDT7 XX_YY  00042    00099\n"
        b"END\n"
    )
    rec = extract_uniprot_map(pdb)["A"]
    assert rec.uniprot_accession == "A0A2K5QDT7"
    assert rec.uniprot_res_begin is None
    assert rec.uniprot_res_end is None


def test_a_long_entry_name_does_not_cost_a_conformant_range():
    """The gate must not refuse a line whose numbers never moved.

    A 13-character entry name grows one column past its own 12-wide field,
    into the separator at index 54 — but the range columns after it stay
    exactly where the spec puts them. An earlier version of the gate also
    required index 54 to be blank, read that as "the columns moved", and
    turned a correct 42..99 into None. Refusing a read the previous reader
    got right is a regression, not caution.

    This is the complement of the parametrised test below: that one pins
    what the gate must refuse, this one what it must NOT.
    """
    pdb = (
        b"HEADER    long entry name, numbers at spec columns\n"
        b"DBREF  1ABC A    1   129  UNP    P00698   ABCDEFGHIJKLM   42     99\n"
        b"END\n"
    )
    rec = extract_uniprot_map(pdb)["A"]
    assert rec.uniprot_accession == "P00698"
    assert (rec.uniprot_res_begin, rec.uniprot_res_end) == (42, 99)


@pytest.mark.parametrize(
    "shape,dbref,accession",
    [
        # Accession under-padded by two columns, pulling every later column
        # LEFT. Before this change the line was dropped at the format check
        # (line[33:41] reads "P00698 L"), so it is one this widened read
        # newly admits — the case the gate exists for. Ungated it records
        # 19..447 against a line saying 1119..1447. Kills a length test on
        # the accession, which is 6 characters here and opens the gate.
        (
            "under-padded accession",
            b"DBREF  1HEW A    1   129  UNP    P00698 LYC_CHICK    1119    1447",
            "P00698",
        ),
        # Accession overflows its field. Its shift happens to leave column 55
        # blank, so an alignment probe on that column is satisfied and reads
        # 42 and 99 off columns that have moved.
        (
            "overflow that lands a blank on column 55",
            b"DBREF  1ABC A    1   130  UNP    A0A2K5QDT7 XX_YY         42     99",
            "A0A2K5QDT7",
        ),
    ],
)
def test_a_newly_admitted_line_never_carries_a_residue_range(shape, dbref, accession):
    """Two lines the old reader dropped, now parsed but without a range.

    Both have an accession that does not sit inside cols 34-41 — one
    under-padded so the slice runs into the entry name, one overflowing —
    so the old reader failed its format check and produced no record at all.
    Reading their range columns would be inventing a number: between them
    they defeat a length test on the accession and an alignment probe on a
    single column, either of which would open the gate on one of them.

    NOT a claim that moved columns are never read. A line whose accession
    does sit in its field is read exactly as it always was, wrong reads
    included; see ``extract_uniprot_map``'s docstring. The guarantee is
    about lines this change newly admits, nothing wider.
    """
    rec = extract_uniprot_map(b"HEADER    " + shape.encode() + b"\n" + dbref + b"\nEND\n")["A"]
    assert rec.uniprot_accession == accession, shape
    assert rec.uniprot_res_begin is None, shape
    assert rec.uniprot_res_end is None, shape


def test_blank_accession_does_not_promote_the_next_column():
    """An empty accession field must stay empty.

    Reading to the next space is what lets a 10-character accession through,
    but splitting on whitespace rather than on a single space walks past a
    blank field to the next token. An entry name would not expose that —
    ``_UNIPROT_RE`` rejects it for its underscore, so the assertion holds
    either way. The token here is a REAL accession that the format check
    waves through, so only the parser can keep it out.
    """
    pdb = (
        b"HEADER    blank accession\n"
        b"DBREF  1XYZ A    1   215  UNP             P00698       123    337\n"
        b"END\n"
    )
    assert extract_uniprot_map(pdb) == {}


def test_multi_chain_dbref():
    pdb = (
        b"HEADER    Multi-chain\n"
        b"DBREF  1ABC A    1   100  UNP    P11111   PROT_A         1    100\n"
        b"DBREF  1ABC B    1   100  UNP    Q22222   PROT_B         1    100\n"
        b"END\n"
    )
    m = extract_uniprot_map(pdb)
    assert m["A"].uniprot_accession == "P11111"
    assert m["B"].uniprot_accession == "Q22222"


def test_lookup_helper():
    pdb = (
        b"HEADER\n"
        b"DBREF  1ABC A    1   100  UNP    P11111   PROT_A         1    100\n"
        b"END\n"
    )
    assert lookup_uniprot_for_chain(pdb, "A") == "P11111"
    assert lookup_uniprot_for_chain(pdb, "Z") is None


def test_alphafold_api_url_valid():
    assert alphafold_api_url("P25779") == (
        "https://alphafold.ebi.ac.uk/api/prediction/P25779"
    )
    # A0A123B4C5-style accession also works.
    assert alphafold_api_url("A0A123B4C5") == (
        "https://alphafold.ebi.ac.uk/api/prediction/A0A123B4C5"
    )


def test_alphafold_api_url_rejects_garbage():
    with pytest.raises(ValueError):
        alphafold_api_url("not-an-accession")


# ---------------------------------------------------------------------------
# Real-PDB integration
# ---------------------------------------------------------------------------

def test_3iut_maps_to_p25779():
    p = PDB_DIR / "hcruz_3iutclean.pdb"
    if not p.exists():
        pytest.skip(f"missing fixture: {p}")
    m = extract_uniprot_map(p.read_bytes())
    assert m["A"].uniprot_accession == "P25779"


def test_3kku_maps_to_p25779():
    p = PDB_DIR / "hcruz_3kku.pdb"
    if not p.exists():
        pytest.skip(f"missing fixture: {p}")
    m = extract_uniprot_map(p.read_bytes())
    assert m["A"].uniprot_accession == "P25779"


def test_af_p24807_maps_to_p24807():
    p = PDB_DIR / "ledogen_AF-P24807-F1-model_v6 (1).pdb"
    if not p.exists():
        pytest.skip(f"missing fixture: {p}")
    m = extract_uniprot_map(p.read_bytes())
    # AlphaFold PDB files carry a DBREF pointing at the UniProt id they
    # were modelled from.
    assert "A" in m
    assert m["A"].uniprot_accession == "P24807"
