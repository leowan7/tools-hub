"""Map a PDB chain to its UniProt accession via DBREF records.

Used by the tools-hub preflight panel to power the "Use AlphaFold model
instead" suggestion: when the user uploads a crystal structure that
needs heavy cleanup (multi-altloc, disordered loops, etc.), we look at
the DBREF records to find the UniProt accession of the target chain and
offer a one-click swap to the AlphaFold-DB model.

This module is intentionally a small standalone reader. It does NOT
parse the full PDB header, does NOT call out to UniProt or AlphaFold-DB
(``alphafold_url`` only builds the URL string), and does NOT mutate
input bytes. The tools-hub side fetches the actual AlphaFold PDB in a
separate request after the user clicks the suggestion.

Vendored byte-identical into ``tools-hub/shared/uniprot_lookup.py``;
see ``tools-hub/contracts/__init__.py`` for the sync rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# AlphaFold-DB REST endpoint that returns the latest model URL for a
# given UniProt accession. We hit ``/api/prediction/{accession}`` and
# read the ``pdbUrl`` field rather than guessing model_v4 / v5 / v6.
ALPHAFOLD_API_BASE = "https://alphafold.ebi.ac.uk/api/prediction"

# UniProt accession regex (covers P12345, A0A123B4C5, Q12345-2 etc.).
# Per UniProt: [OPQ][0-9][A-Z0-9]{3}[0-9] | [A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}
_UNIPROT_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})"
    r"(?:-\d+)?$"
)


@dataclass
class ChainUniProtMap:
    """One DBREF record's mapping: PDB chain X residues a..b ↔ UniProt P12345 residues c..d."""
    chain_id: str
    pdb_res_begin: int
    pdb_res_end: int
    uniprot_accession: str
    uniprot_res_begin: Optional[int] = None
    uniprot_res_end: Optional[int] = None


def extract_uniprot_map(data: bytes) -> dict[str, ChainUniProtMap]:
    """Walk DBREF records in the PDB bytes and return a chain→UniProt map.

    Returns at most one entry per chain (the first DBREF for that chain
    referencing UNP wins). Chains without a UniProt-typed DBREF are
    absent from the dict.

    DBREF format (PDB v3.3, fixed-width):
        cols  1-6   "DBREF "
        col   13    chain ID
        cols 15-18  PDB seq begin (int)
        cols 21-24  PDB seq end (int)
        cols 27-32  database name (UNP for UniProt, GB, REF, etc.)
        cols 34-41  database accession (e.g. P12345)
        cols 56-60  database seq begin (optional)
        cols 63-67  database seq end (optional)

    DBREF1/DBREF2 continuation records (used for accessions > 8 chars)
    are NOT handled — they're vanishingly rare in target PDBs and the
    fallback ("use a fresh upload") is fine when they appear.
    """
    out: dict[str, ChainUniProtMap] = {}
    for raw in data.split(b"\n"):
        if not raw.startswith(b"DBREF "):
            continue
        try:
            line = raw.decode("ascii", errors="replace").rstrip("\r")
        except Exception:
            continue
        if len(line) < 41:
            continue
        chain = line[12] if len(line) > 12 else " "
        db = line[26:32].strip() if len(line) >= 32 else ""
        if db != "UNP":
            continue
        accession = line[33:41].strip()
        if not _UNIPROT_RE.match(accession):
            continue
        try:
            pdb_begin = int(line[14:18])
            pdb_end = int(line[20:24])
        except (ValueError, IndexError):
            continue
        # UniProt residue range is optional in our reader.
        uni_begin: Optional[int] = None
        uni_end: Optional[int] = None
        try:
            uni_begin = int(line[55:60].strip())
            uni_end = int(line[62:67].strip())
        except (ValueError, IndexError):
            pass
        if chain in out:
            # First DBREF for this chain wins (matches typical RCSB
            # convention where the first record covers the longest range).
            continue
        out[chain] = ChainUniProtMap(
            chain_id=chain,
            pdb_res_begin=pdb_begin,
            pdb_res_end=pdb_end,
            uniprot_accession=accession,
            uniprot_res_begin=uni_begin,
            uniprot_res_end=uni_end,
        )
    return out


def lookup_uniprot_for_chain(data: bytes, target_chain: str) -> Optional[str]:
    """Convenience: return the UniProt accession for the named chain, or None."""
    m = extract_uniprot_map(data)
    rec = m.get(target_chain)
    return rec.uniprot_accession if rec else None


def alphafold_api_url(uniprot: str) -> str:
    """Build the AlphaFold-DB prediction API URL for an accession.

    The response is a JSON list; the first element has ``pdbUrl`` /
    ``cifUrl`` keys pointing to the latest available model files (v4/v5/v6
    depending on when the entry was last refreshed). Callers should hit
    this URL with a short timeout and follow ``pdbUrl`` to the actual
    coordinate file.
    """
    if not _UNIPROT_RE.match(uniprot):
        raise ValueError(f"Not a valid UniProt accession: {uniprot!r}")
    return f"{ALPHAFOLD_API_BASE}/{uniprot}"
