"""pLDDT in the B-factor column, on the scale the field reads it on.

A predicted structure carries its per-residue confidence in the
B-factor column. AlphaFold DB and the ESM Metagenomic Atlas both write
it on **0-100**, and every recipe a structural biologist reaches for
assumes that -- ``spectrum b, blue_white_red, minimum=50, maximum=90``
in PyMOL, the equivalent in ChimeraX and Mol*.

ESMFold's HuggingFace head returns pLDDT on 0-1, and
``model.output_to_pdb`` writes whatever it is handed. So a downloaded
ESMFold structure has B-factors running 0.10-0.77 where the page above
the download button reads 21.5-65.9, and colouring it by the usual
breakpoints paints the whole chain one flat colour.

This module converts on the way OUT, next to
``shared.metric_glossary.plddt_on_100`` which does the same job for the
numbers. Nothing stored is rewritten: the jobs table holds completed
runs whose PDBs no pipeline change could reach, and a pipeline fix would
split the convention silently -- files written before it on one scale,
after it on the other, with nothing in either to say which.

WHY THE GATE IS WHOLE-FILE AND NOT PER-ATOM. A real crystallographic
B-factor can be below 1. ``static/example/1HEW.pdb``, hen egg-white
lysozyme, runs **0.01 to 150.80**: a per-atom rule would scale that one
0.01 atom to 1.00 and leave the 150.80 alone, corrupting a file it had
no business touching. A file whose MAXIMUM B-factor is at or below 1 is
not a refined crystal structure; it is a confidence written as a
fraction. So the decision is made once for the whole file, and a file
that fails the test is returned byte-for-byte unchanged.

WHY ONE PREDICATE DECIDES WHAT A COORDINATE RECORD IS. The first
version had the reader use ``splitlines()`` and the writer
``splitlines(keepends=True)``, both testing length against 66. A
65-character record plus its newline measures 66, so the writer
rewrote a line the reader had never inspected -- and since
``line[66:]`` was then the empty string, it ate the terminator and
welded two ATOM records into one. Reader and writer now share
``_coordinate_bfactor``, and neither can see a line the other cannot.

WHY THE CHECK IS THE WHOLE COLUMN LAYOUT AND NOT THE RECORD NAME.
``ATOM  `` also prefixes an mmCIF ``_atom_site`` row, which is
whitespace-DELIMITED: rewriting fixed offsets in one eats a separator
and yields a row with a field missing. A PDB coordinate record is a
fixed-column format, so the predicate reads it as one -- x, y, z,
occupancy and B all have to parse at their own offsets before this
module will touch the line.
"""

from __future__ import annotations

import base64

# Zero-indexed slices of the PDB coordinate record. A PDB is a column
# format, not a delimited one, so the slice IS the parse.
_X = (30, 38)
_Y = (38, 46)
_Z = (46, 54)
_OCCUPANCY = (54, 60)
_B_START = 60
_B_END = 66

_COORD_RECORDS = ("ATOM  ", "HETATM")


def _coordinate_bfactor(content: str) -> float | None:
    """The B-factor of ``content``, or None if it is not a PDB record.

    ``content`` must already have its line terminator stripped -- that
    is the whole point of routing every caller through here.
    """
    if not content.startswith(_COORD_RECORDS) or len(content) < _B_END:
        return None
    try:
        for start, end in (_X, _Y, _Z, _OCCUPANCY):
            float(content[start:end])
        return float(content[_B_START:_B_END])
    except ValueError:
        return None


def _lines(pdb_text: str):
    """``(content, terminator)`` per line, terminator preserved."""
    for raw in pdb_text.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        yield content, raw[len(content):]


def bfactors(pdb_text: str) -> list[float]:
    """Every B-factor this module considers in scope, in order."""
    out = []
    for content, _term in _lines(pdb_text):
        value = _coordinate_bfactor(content)
        if value is not None:
            out.append(value)
    return out


def is_fractional(pdb_text: str) -> bool:
    """True when this file's B-factors are a 0-1 confidence.

    Whole-file: every B-factor must be within 0-1. One atom above it
    and the file is left alone, because a refined structure is what
    that looks like.
    """
    values = bfactors(pdb_text)
    if not values:
        return False
    return all(0.0 <= v <= 1.0 for v in values)


def bfactors_on_100(pdb_text: str) -> str:
    """``pdb_text`` with a 0-1 B-factor column scaled to 0-100.

    Returns the SAME object when the file is not fractional, has no
    coordinate records, or is empty. Callers rely on that identity to
    avoid re-encoding bytes they did not change.

    NOT idempotent, and the column's own precision fixes where that
    bites: a B-factor is written to two decimals, so the smallest
    non-zero value a file can hold is 0.01, and 0.01 scales to exactly
    1.00 -- still inside the fractional window, so a second pass takes
    it to 100. Apply exactly once. Real predictions do not live there
    (the disordered example on this site bottoms out at 0.10) but the
    property is not guaranteed and must not be claimed.
    """
    if not pdb_text or not is_fractional(pdb_text):
        return pdb_text

    out = []
    for content, term in _lines(pdb_text):
        value = _coordinate_bfactor(content)
        if value is None:
            out.append(content + term)
            continue
        # "%6.2f" is the column's own width and precision. The gate
        # proved every value is at or below 1, so the largest this can
        # produce is 100.00, which fills the field exactly.
        out.append(
            f"{content[:_B_START]}{value * 100:6.2f}{content[_B_END:]}{term}"
        )
    return "".join(out)


def bfactors_on_100_b64(pdb_b64: str) -> str:
    """The same, base64 in and base64 out, for a ``data:`` URI.

    Anything that does not decode to text with coordinate records comes
    back untouched: a download that renders a wrong scale is a bug, and
    a download that renders nothing is an outage.
    """
    if not pdb_b64:
        return pdb_b64
    try:
        text = base64.b64decode(pdb_b64, validate=True).decode("utf-8")
    except Exception:
        return pdb_b64
    converted = bfactors_on_100(text)
    if converted is text:
        return pdb_b64
    return base64.b64encode(converted.encode("utf-8")).decode("ascii")


def bfactors_on_100_bytes(pdb_bytes: bytes) -> bytes:
    """The same for raw bytes, as a download route holds them.

    Returns the SAME object unless the text actually changed. The first
    version decoded and re-encoded unconditionally, which destroyed
    every non-UTF-8 byte in a file the gate had already declined --
    breaking the byte-for-byte promise on exactly the files it exists
    to protect.
    """
    if not pdb_bytes:
        return pdb_bytes
    try:
        text = pdb_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return pdb_bytes
    converted = bfactors_on_100(text)
    if converted is text:
        return pdb_bytes
    return converted.encode("utf-8")
