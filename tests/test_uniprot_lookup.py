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
