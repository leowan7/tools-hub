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
"""

from __future__ import annotations

import base64

# Columns 61-66 in the PDB spec, zero-indexed. Fixed-width; a PDB is a
# column format, not a delimited one, so the slice is the parse.
_B_START = 60
_B_END = 66
_COORD_RECORDS = ("ATOM  ", "HETATM")


def _bfactors(pdb_text: str) -> list[float]:
    """Every parseable B-factor in the file, in order."""
    out = []
    for line in pdb_text.splitlines():
        if line.startswith(_COORD_RECORDS) and len(line) >= _B_END:
            try:
                out.append(float(line[_B_START:_B_END]))
            except ValueError:
                continue
    return out


def is_fractional(pdb_text: str) -> bool:
    """True when this file's B-factors are a 0-1 confidence.

    Whole-file: every B-factor must be within 0-1. One atom above it and
    the file is left alone, because a refined structure is what that
    looks like.
    """
    values = _bfactors(pdb_text)
    if not values:
        return False
    return all(0.0 <= v <= 1.0 for v in values)


def bfactors_on_100(pdb_text: str) -> str:
    """``pdb_text`` with a 0-1 B-factor column scaled to 0-100.

    Returned unchanged when the file is not fractional, when it has no
    coordinate records, or when it is empty.

    NOT idempotent, for the same reason
    ``metric_glossary.plddt_on_100`` is not: a file whose B-factors are
    all at or below 0.01 scales into a range that is still fractional,
    so a second pass would scale it again. Apply exactly once. Real
    predicted structures do not live there -- the disordered example on
    this site bottoms out at 0.10 -- but the property is not guaranteed
    and must not be claimed.
    """
    if not pdb_text or not is_fractional(pdb_text):
        return pdb_text

    out = []
    for line in pdb_text.splitlines(keepends=True):
        if not line.startswith(_COORD_RECORDS) or len(line) < _B_END:
            out.append(line)
            continue
        try:
            value = float(line[_B_START:_B_END])
        except ValueError:
            out.append(line)
            continue
        # "%6.2f" is the column's own width and precision. A value of
        # 100.00 fills it exactly; nothing here can exceed that, since
        # the gate proved every value is at or below 1.
        out.append(f"{line[:_B_START]}{value * 100:6.2f}{line[_B_END:]}")
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
