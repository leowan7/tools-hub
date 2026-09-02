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

# How far into a file an mmCIF marker can hide. A CIF header is a
# handful of lines; 16 KB is slack, not a guess at pathological
# input. Sliced off the STRING, because splitlines()[:200] built the
# whole line list before taking 200 of it -- bounding the loop and
# not the split, which left an O(n) pass in front of the
# short-circuit it was meant to stop competing with.
_CIF_HEADER_BYTES = 16384

# A byte-order mark ahead of the first record. It is not whitespace to
# Python, so it does not strip and it is not a coordinate-record prefix:
# a BOM'd first ATOM line was invisible to the gate AND to the writer,
# which is not "declined", it is HALF CONVERTED. A file reading
# 0.50/0.10/0.77 was staged as 0.50/10.00/77.00 -- the same one-chain-
# converted shape the fail-closed rule exists to prevent, arriving
# through the one door that rule does not watch. Stripped once at the
# entry point so the predicate and the writer see the same text; a
# converted file loses the mark, which a PDB should not have carried.
_BOM = "\ufeff"


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
    """``(content, terminator)`` per line, terminator preserved.

    The split and the strip MUST agree on what a line ending is, and
    hard-coding ``\r\n`` did not. ``str.splitlines`` also breaks on
    ``\x0b \x0c \x1c \x1d \x1e \x85 \u2028 \u2029``, so a 65-character
    coordinate record terminated by one of those came back as 66
    characters with the separator sitting in column 66 -- and
    ``float()`` accepts most of them as trailing whitespace, so the B
    field parsed, the writer overwrote all six columns, and the
    terminator was eaten. Two ATOM records welded into one 144-character
    line.

    That is the same weld the module docstring above says was fixed.
    Routing reader and writer through one predicate closed the 66-vs-65
    LENGTH half of it; this closes the SEPARATOR half. Splitting each
    raw line with the same function that produced it is symmetric by
    construction, which hard-coding a terminator set never is.
    """
    for raw in pdb_text.splitlines(keepends=True):
        content = raw.splitlines()[0]
        yield content, raw[len(content):]


def bfactors(pdb_text: str) -> list[float]:
    """Every B-factor this module considers in scope, in order."""
    out = []
    for content, _term in _lines(pdb_text):
        value = _coordinate_bfactor(content)
        if value is not None:
            out.append(value)
    return out


def _looks_like_cif(pdb_text: str) -> bool:
    """A line-anchored mmCIF marker, the way the viewer sniffs format.

    The column-layout check already refuses every real CIF writer I
    could find, but it is a heuristic over offsets and a hand-rolled
    row can align by chance. opendde stores a ``.cif`` under
    ``pdb_key`` when its CIF-to-PDB conversion fails
    (tools/opendde/run_pipeline.py:456), so a CIF genuinely reaches
    the download routes. Cheap insurance; mirrors detectFormat in
    static/js/mol_viewer.js.
    """
    # Bounded: a CIF declares itself in its header. ``data_`` is the
    # first non-blank line and the ``_atom_site.`` loop precedes the
    # rows. Scanning the whole file put an O(n) pass in front of the
    # short-circuit below and halved its benefit on a 671 KB archive
    # member -- the check has to be cheaper than the thing it guards.
    for line in pdb_text[:_CIF_HEADER_BYTES].splitlines():
        if line.lstrip().startswith(("data_", "_atom_site.", "loop_")):
            return True
    return False


def is_fractional(pdb_text: str) -> bool:
    """True when this file's B-factors are a 0-1 confidence.

    Whole-file: every B-factor must be within 0-1. One atom above it
    and the file is left alone, because a refined structure is what
    that looks like.

    FAILS CLOSED ON AMBIGUITY, which the first version of this
    predicate did not. It read B-factors through the same layout check
    the writer uses, so a line that LOOKS like a coordinate record but
    does not parse -- a target chain carried over from a deposition
    with a blank occupancy field, say -- was silently skipped rather
    than counted. Skipping is not disqualifying: a B-factor of 49 on
    such a line could no longer veto the conversion, and a stitched
    binder-plus-target complex came out with 11.00 on one chain and
    49.0 on the other. If this module cannot read a line it can see,
    it does not get to make a whole-file judgement about the file.

    Short-circuits on the first disqualifying value, which keeps the
    cost off af2, colabfold and pxdesign: they bail on their first ATOM
    record instead of scanning a 2944-design archive for a conclusion
    available on line one. The saving is real but not total -- ``_lines``
    builds the whole line list before it yields, so a decline still pays
    one O(n) split. Measured at ~1.4 ms/MB against ~40 ms/MB to convert,
    so it is worth stating accurately rather than fixing.

    KNOWN CEILING, deliberately not closed: a file whose B column is a
    uniform placeholder ``1.00`` carrying no confidence at all passes
    this gate and is promoted to a uniform ``100.00`` -- fake perfect
    confidence, which is worse than the flat colouring the module
    exists to fix. Declining "every B-factor identical" would catch it,
    but that is a guess about INTENT bolted onto a rule that is
    otherwise a fact about FORMAT, and no writer in this repo is known
    to emit one: all three ``static/example`` depositions have 871+
    distinct values. Left as one rule until something real produces the
    input. Uniform ``0.00`` is already a no-op.
    """
    pdb_text = pdb_text.lstrip(_BOM)
    if _looks_like_cif(pdb_text):
        return False
    seen = False
    for content, _term in _lines(pdb_text):
        if not content.startswith(_COORD_RECORDS):
            continue
        # ONE RULE. If a line claims to be a coordinate record and this
        # module cannot read its B-factor -- blank occupancy, a field
        # truncated at 63 characters, coordinates run together by an
        # overflow -- the file does not get judged. An earlier version
        # skipped those instead, and a 49.00 the reader could not parse
        # therefore could not veto: a stitched complex converted the
        # binder and left the target, 11.00 beside 49.0 in one file.
        #
        # The length special-cases that used to sit here were
        # redundant. _coordinate_bfactor already returns None for a
        # record too short to hold a B field, and the rule is easier to
        # trust with nothing in front of it.
        value = _coordinate_bfactor(content)
        if value is None or not 0.0 <= value <= 1.0:
            return False
        seen = True
    return seen


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
    if not pdb_text:
        return pdb_text
    body = pdb_text.lstrip(_BOM)
    if not is_fractional(body):
        # The ORIGINAL object, never the stripped copy: callers use
        # identity to decide whether to re-encode, and handing back a
        # BOM-less twin would make every declined file look changed.
        return pdb_text

    out = []
    for content, term in _lines(body):
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
