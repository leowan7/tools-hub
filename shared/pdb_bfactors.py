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
numbers. No SOURCE OF TRUTH is rewritten: the jobs table holds completed
runs whose PDBs no pipeline change could reach, and a pipeline fix would
split the convention silently -- files written before it on one scale,
after it on the other, with nothing in either to say which.

One caller is an exception worth knowing about before you read the rest.
``shared.storage.stage_campaign_candidates`` copies a shortlist into the
``lab-campaigns`` bucket for wet-lab handoff and PERSISTS what this
module returns, because nothing in the app reads that bucket back and
there is no read-time seam to convert at. The original in
``tool-outputs`` is still untouched, so the paragraph above holds for
the source of truth -- but "nothing stored is rewritten" is no longer
true of every byte this module produces.

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

# WHY A LINE THAT IS NOT A COORDINATE RECORD CAN STILL DISQUALIFY.
#
# The gate below skips lines failing ``startswith(_COORD_RECORDS)``,
# which is right for REMARK, TER and the rest -- but a coordinate record
# carrying one invisible byte in front of it fails that test too, and
# was skipped as though it were prose. Skipping is not disqualifying, so
# the value on that record could not veto: a file holding a real 88.50
# was judged fractional because the record carrying it was never
# counted, and came out 10.00 beside 88.50.
#
# ONLY A HIDDEN COORDINATE RECORD DISQUALIFIES. An invisible byte on a
# line that was never going to hold a B-factor -- a BOM in front of
# HEADER or CRYST1, a NUL pad, a DOS Ctrl-Z at end of file -- is
# harmless, and declining those was a silent regression to the 0-1
# scale on files that had converted correctly for months. Worse, it hit
# the motivating case: ``cat binder.pdb target.pdb`` puts the mark on
# the SECOND file's first line, and a PDB that opens with HEADER or
# CRYST1 wears it somewhere harmless. Not all of them do -- of the
# three depositions in static/example, two open with HEADER and
# 3s7g_fc_ab.pdb opens with a bare ATOM record, where declining was
# right. An earlier version of this note said "every real PDB", which
# is the kind of universal that is always one counterexample from
# false, and the counterexample was already in the repo.
#
# Fail-closed earns its keep on a record this module can see and cannot
# read; it does not license declining a line it was always going to
# skip.
#
# WHAT COUNTS AS INVISIBLE, and the ceiling on it. Every earlier
# attempt here was a LIST, and each one missed the tier below it: a BOM
# at the file head missed every other offset; four named characters
# missed the no-break space, the zero-width space, the word joiner, the
# ideographic space and ZWNJ; a Unicode character class still missed a
# swathe of control and format codepoints; and inspecting ``content[0]``
# alone missed anything behind a single leading space, which is a list
# of POSITIONS rather than of characters.
#
# (Earlier revisions of this note put a count on each tier. Those
# numbers came from mutants nobody will run again, one of them was
# arithmetically impossible -- it described a figure as "all C0/C1
# controls" when there are only 65 Cc codepoints in the whole of
# Unicode -- and none is reproducible from anything committed. The
# shape is the lesson; the tallies were decoration that had to be
# defended.)
#
# A list has to be complete to be correct and there is no way to know
# that it is. So the predicate is ``not str.isprintable()`` plus the
# ASCII space -- the one WHITESPACE character Python calls printable --
# applied over the WHOLE prefix rather than its first character.
# ("Invisible" would be wrong and the next paragraph says why: Python
# calls roughly two thousand zero-width characters printable too. Only
# the whitespace claim is the true one, and only it is needed here.)
#
# That covers every control, format, surrogate, private-use, unassigned
# and separator codepoint. It does NOT cover characters that are
# ASSIGNED and printable but render blank or take no width. Two
# families: the Default_Ignorable set -- variation selectors, the
# Hangul fillers, the Mongolian free variation selectors, U+034F
# COMBINING GRAPHEME JOINER -- together with U+2800 BRAILLE PATTERN
# BLANK, and separately every zero-advance combining mark, which is
# much the larger of the two families.
#
# BE PRECISE ABOUT WHAT THAT COSTS. It is not "a line is skipped": the
# file comes out HALF CONVERTED, 10.00 beside 88.50, on the live
# customer download path. Accepted anyway, because deciding which
# glyphs look blank is a judgement about typography rather than about
# encoding, and it would be one more list of the kind this module has
# already been wrong with. The realistic way in is a human pasting
# text carrying a variation selector. Pinned by tests naming both
# families, so closing it is a deliberate act.
#
# (A previous revision put "exactly 268" here. That is the right count
# for Default_Ignorable plus U+2800 and the wrong count for the class
# the sentence describes, because it silently dropped a combining-mark
# clause the revision before it had. A number in a comment is a claim
# somebody has to keep true through every later edit, and the numbers
# in this file have now been wrong more often than the code has.)


def _visible_start(content: str) -> str:
    """``content`` from its first visible character.

    Recognition only -- nothing this returns is ever written. The file
    it came from is either declined or untouched.

    UNBOUNDED ON PURPOSE, and that costs something. It walks the whole
    invisible prefix, so it is O(prefix) per non-coordinate line: a
    header-heavy file roughly doubles the gate's cost, and a single
    pathological all-NUL line of half a megabyte takes tens of
    milliseconds where the old first-character check took under one.
    Linear, so nothing can hang, and the request is already capped by
    MAX_CONTENT_LENGTH -- but a maximally-invisible payload at that cap
    is on the order of a second of worker CPU. Capping the loop is the
    obvious saving and it is exactly wrong: a cap is a length, and a
    record behind a longer run walks straight past the gate. Reviewers
    have found a cap surviving the whole suite more than once, at
    different lengths each time, which is the argument against caps
    rather than against any particular one. If this ever needs to be
    cheaper, bound the number of LINES inspected, never the prefix.
    """
    index = 0
    while index < len(content) and (
        content[index] == " " or not content[index].isprintable()
    ):
        index += 1
    return content[index:]


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
        # Same helper as the record check below, so the two recognisers
        # cannot disagree about what a line begins with: a CIF saved
        # with a BOM is exactly as much a CIF as one without.
        #
        # It is WIDER than the lstrip() it replaced, and measurably
        # dearer -- a Python-level loop where there was a C-level
        # strip, about +0.02 ms on a 132 KB header. The comment above
        # asks this check to be cheaper than the thing it guards and it
        # still is by two orders of magnitude, but the trade is real
        # and the widening is behaviour: a NUL in front of ``loop_``
        # now sniffs as CIF and declines. Fail-closed, and pinned.
        if _visible_start(line).startswith(
            ("data_", "_atom_site.", "loop_")
        ):
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

    NOT ABSOLUTE, and the exception is documented rather than implied.
    The blank-rendering and zero-width codepoints described in the note
    above ``_visible_start`` still hide a record from this predicate,
    and a file behind one comes out half converted. Everything else
    this module says about the shape being impossible should be read
    as "impossible
    except there", which is the whole reason that ceiling is pinned by
    a test.

    Short-circuits on the first disqualifying value, which keeps the
    cost off af2, colabfold and pxdesign: they bail on their first ATOM
    record instead of scanning a 2944-design archive for a conclusion
    available on line one. The saving is real but not total -- ``_lines``
    builds the whole line list before it yields, so a decline still pays
    one O(n) split. Measured on the Fc dimer in static/example: of
    order 1 ms/MB to decline against of order 20 ms/MB to convert, so
    declining is roughly twenty times cheaper. Three revisions of this
    docstring carried three different pairs of figures, and reviewers
    measured a fourth and a fifth on their own machines -- which is the
    argument for stating the RATIO and an order of magnitude rather
    than digits that read as precision nobody can reproduce.

    KNOWN CEILING, deliberately not closed: a file whose B column is a
    uniform placeholder ``1.00`` carrying no confidence at all passes
    this gate and is promoted to a uniform ``100.00`` -- fake perfect
    confidence, which is worse than the flat colouring the module
    exists to fix. Declining "every B-factor identical" would catch it,
    but that is a guess about INTENT bolted onto a rule that is
    otherwise a fact about FORMAT. The design paths hand through
    whatever the model wrote rather than composing a B-factor column
    themselves. The writers that exist: ``sdf_to_pdb`` in
    ``tools/proteina/run_pipeline.py`` via RDKit's ``MolToPDBFile``,
    which has no call site; Biopython's ``PDBIO().save()`` in
    ``shared/pipeline_normalize`` and ``shared/pdb_inspect``, which
    re-serialise a structure parsed from somebody else's file rather
    than composing a B column; and ``_FALLBACK_PDB`` in
    ``tools/proteina/_hotspot_canary.py``, which DOES hand-compose one
    -- a uniform ``0.00``, the harmless half of this ceiling -- and is
    canary-only, reaching no download path. A reviewer found that last
    one missing from an earlier version of this list, in a paragraph
    reasoning about exactly that. What column RDKit
    would put there is NOT asserted here: rdkit is not installed in
    this repo's venv and no test exercises that function, so it is not
    something a reader can check.

    An earlier version of this sentence said ``sdf_to_pdb`` was "the
    one place in the tree that writes a PDB from scratch", which is the
    same shape of universal this module warns about two hundred lines
    up, written two hundred lines later. Enumerating is safer than
    claiming uniqueness, and shorter than defending it.

    That hedge is the point. This verdict has now been justified
    several different ways and the earlier ones were wrong -- one
    argued about WRITERS from the distinct-value counts of three
    DOWNLOADED depositions, another claimed no ``run_pipeline`` writes
    a B-factor field at all -- each caught by a reviewer. (An earlier
    revision numbered them, and the number was itself wrong at every
    revision that carried it, which is its own small lesson.) A verdict
    that survives repeated bad arguments is one to state carefully,
    not one to keep re-justifying. Left as one rule until something real produces
    the input. Uniform ``0.00`` is already a no-op.
    """
    if _looks_like_cif(pdb_text):
        return False
    seen = False
    for content, _term in _lines(pdb_text):
        if not content.startswith(_COORD_RECORDS):
            # One question: is this a coordinate record wearing an
            # invisible prefix? Then it is a record this module can see
            # and cannot read, and the file is declined. Anything else
            # -- a BOM before HEADER, a NUL pad, a stray control byte
            # on a REMARK -- is a line that holds no B-factor and was
            # always going to be skipped. See the note by
            # _COORD_RECORDS for why this is a predicate, not a list.
            if _visible_start(content).startswith(_COORD_RECORDS):
                return False
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
