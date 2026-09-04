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

Originally vendored from llm-proteinDesigner's backend/pdb_utils/.
Whether it still matches that copy is UNKNOWN here and not checkable
from this repo: ``.github/workflows/vendored-drift.yml`` is the only
rule, and it only tripwires changes to this file against
``shared/VENDORED_SHA256.lock`` — it never fetches the sibling. (Its
own comment records measuring drift in pipeline_normalize.py; nothing
has measured this file.) Refresh the lock in the same commit as any
change here, and treat the sibling as a separate human decision.
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
        cols 34-..  database accession (e.g. P12345), read to the next space
        cols 56-60  database seq begin (optional)
        cols 63-67  database seq end (optional)

    The accession field is 8 wide in the spec, but AlphaFold DB writes
    10-character accessions straight through it and lets the entry name
    shift right, so the accession is read to the next space instead:

        DBREF  XXXX A    1   130  UNP    A0A2K5QDT7 A0A2K5QDT7_CEBIM     1    130

    The optional seq begin/end columns move with it, by the accession's
    overflow PLUS any entry-name overflow — 6 columns on the line above,
    not 2, because A0A2K5QDT7_CEBIM is 16 characters in a 12-wide field.

    They are therefore read only when ``line[33:41].strip()`` still equals
    the accession, which is exactly the expression the OLD reader used to
    find it. An equal slice means the old reader found this same accession,
    so such a line is read precisely as it always was — wrong reads
    included: one extra pad space still turns 3AVE's 108..330 into 10..33,
    before and after alike. No attempt is made to do better than that; a
    fixed-width record does not reliably say whether its columns moved, and
    an earlier gate that guessed refused correct reads.

    The converse does not hold, and that is the cost, in two different
    sizes. An accession that does not start at column 34 is no longer found
    at all, so its line yields NO RECORD where the old reader gave one. An
    accession that overflows the field does yield a record, with the
    accession corrected -- which is the point of this change -- but without
    a range: Q12345-12 is nine characters, so the old reader truncated it to
    Q12345-1 and read a range off columns it had already mislocated.

    No production code reads either field — ``_maybe_alphafold`` in
    shared/pdb_preflight.py takes only the accession — but
    ``test_dbref_unp_record_parsed`` pins both, so they are not free to
    guess at later.

    DBREF1/DBREF2 continuation records are NOT handled here, though
    ``scout/epitope_db.py`` does read them. The wwPDB uses that two-line
    form when an accession is too wide for the single-line field; AlphaFold
    DB does not, and the entry inspected here, AF-A0A2K5QDT7-F1, overflows
    the field instead, which is what the read above exists for. Nothing
    covers the two-line form when it reaches THIS module: the chain does
    not map, ``_maybe_alphafold`` returns None, the preflight panel omits
    the AlphaFold block, and the upload proceeds as it is.
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
        # Split on a literal space, not on whitespace: a blank accession
        # field must stay blank rather than promote the entry name into it.
        accession = line[33:].split(" ")[0].strip()
        if not _UNIPROT_RE.match(accession):
            continue
        try:
            pdb_begin = int(line[14:18])
            pdb_end = int(line[20:24])
        except (ValueError, IndexError):
            continue
        # UniProt residue range is optional in our reader, and read only when
        # the accession sits wholly inside cols 34-41. That condition IMPLIES
        # "the old reader found this same accession": it took
        # line[33:41].strip() and ran the same format check, so an equal
        # slice means an equal result. Such a line is therefore read
        # precisely as it always was, correct where that was correct and
        # wrong where it was not.
        #
        # The converse does NOT hold, and the gate is the stricter side: a
        # 9-character isoform, overflowing the field by one, parsed before
        # too — as a truncation of itself, Q12345-12 read as Q12345-1.
        # Correcting the accession there costs the range, which is the right
        # trade. An 8-character one fits, and is untouched.
        #
        # It is NOT a proof that the columns did not move, and does not try
        # to be. An earlier version also required col 55 to be blank, which
        # refused correct reads: a 13-character entry name grows into that
        # column while the numbers stay at theirs, and 42..99 became None.
        # Detecting a real shift needs each field's intended width, which
        # the bytes do not carry.
        #
        # What it does buy is that a line this change NEWLY admits carries
        # no range at all. Ungated, an accession under-padded by two columns
        # records 19..447 where the line's own columns say 1119..1447.
        uni_begin: Optional[int] = None
        uni_end: Optional[int] = None
        if line[33:41].strip() == accession:
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
