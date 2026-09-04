"""Structural epitope database lookup via SAbDab and RCSB PDB.

Queries SAbDab (Structural Antibody Database) for known antibody/nanobody/VHH
binders to a target protein identified by UniProt accession. For the top
structures by resolution, downloads the PDB coordinate file and computes
antigen–antibody contact residues using BioPython.

Usage:
    from scout.epitope_db import fetch_known_binders
    binders = fetch_known_binders("P00533")  # EGFR

Each returned dict has:
    pdb_id          str   — RCSB PDB accession (uppercase)
    binder_type     str   — "VHH/Nanobody", "IgG/Fab", or "Unknown"
    species         str   — Antigen source organism from SAbDab
    resolution      float — X-ray/cryo-EM resolution in Angstroms (None if NMR)
    affinity        str   — Kd if deposited, else empty string
    contact_residues list[int] — Antigen residue numbers at the interface.
                          ABSENT when the interface was not established: the
                          entry is past ``max_contact_structures`` (permanent,
                          and the common case for a well-studied target), the
                          coordinate host was unreadable or still inside its
                          backoff, the body would not parse, or the download
                          outran the join. A 404 is NOT in that list — an entry
                          with no PDB-format file settles as [].
                          Absent is not zero. ``.get("contact_residues", [])``
                          is safe when you only need residue numbers, but test
                          ``"contact_residues" in entry`` before telling a user
                          anything about the interface, and never write a
                          placeholder [] back.
    antigen_chain   str   — Antigen chain ID in the PDB entry
    ab_chains       list[str] — Antibody chain IDs
"""

import copy
import csv
import difflib
import logging
import re
import threading
import time
from io import StringIO
from typing import Optional

import requests

from scout import polymer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
# SAbDab's WHOLE-DATABASE summary, as one CSV. This replaces the retired
# per-structure endpoint
# (opig.stats.ox.ac.uk/webapps/sabdab-sabpred/sabdab/summary/<pdb_id>/), which
# now 301s to a React SPA and answers an identical ~1457-byte HTML shell for
# every PDB id — so every lookup silently returned "not in SAbDab" and the
# feature had been dead for some time (measured 2026-08-18, Phase 0).
#
# Fetching the whole database once and indexing it in memory is not a
# compromise, it is strictly cheaper than what it replaces: the old code made
# ONE HTTPS request PER candidate PDB id, up to _RCSB_PROBE_LIMIT (40) of them,
# each on its own raw unbounded thread, on EVERY anonymous /analyze. One
# request that is then reused by every subsequent lookup beats 40 that are not.
# The payload is ~11.7 MB of CSV but only ~1.2 MB on the wire (the server
# gzips; requests negotiates and decodes it transparently), and the parsed
# index keeps only the six fields used below.
SABDAB_SUMMARY_URL = "https://sabdab.opig.stats.ox.ac.uk/api/download/all-summary"
# RCSB search API — used to find PDB entries containing a given UniProt entity.
RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_PDB_DOWNLOAD_URL = "https://files.rcsb.org/download/{pdb_id}.pdb"
UNIPROT_FASTA_URL = "https://rest.uniprot.org/uniprotkb/{uniprot_id}.fasta"
# UniProtKB indexes its sequences by CRC64/MD5 checksum, exposed as the
# `checksum:` search field, so an exact sequence resolves in one GET. A real
# similarity search (EBI ncbiblast) is a submit/poll job and needs a budget
# this request path does not have.
UNIPROTKB_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"

# How many RCSB PDB IDs to check against SAbDab before giving up. Higher
# values increase recall. Since the SAbDab side became an in-memory dict
# lookup this costs nothing per id — the only cost is the single RCSB search
# request, whose page size this is.
_RCSB_PROBE_LIMIT = 40

# Timeout for all external HTTP requests.
_REQUEST_TIMEOUT_SEC = 12

# The whole-database summary fetch is bigger than a per-id probe, so it gets
# its own, longer timeout: ~1.2 MB gzipped, measured 1.4-6.4 s depending on
# whether the server serves it precompressed.
_SUMMARY_TIMEOUT_SEC = 60

# Maximum number of structures for which contact residues are computed.
# Each requires a PDB file download (~0.5–5 MB) and BioPython parsing.
_MAX_CONTACT_STRUCTURES = 5

# Contact distance cutoff in Angstroms. 4.5 Å captures hydrogen bonds and
# van der Waals contacts at protein–protein interfaces.
_CONTACT_CUTOFF_ANGSTROM = 4.5

# The official UniProtKB accession grammar, from the UniProt help pages. Both
# the 6-character and 10-character forms.
#
# This is a trust boundary, not a tidiness check. The accession arrives from
# columns 33-41 of a DBREF line in a file the caller uploaded, so without it
# any eight bytes at all become a cache key and a lookup target: `ZZ9QC001`
# was accepted, resolved, and cached. Every entry point that can mint a cache
# key runs a string through here first.
_UNIPROT_ACCESSION_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$"
)


def _valid_accession(accession: str) -> str:
    """Return ``accession`` uppercased if it is a real UniProt accession, else "".

    Rejecting is the right answer rather than passing it through: an
    unparseable accession cannot match anything upstream, so the only thing a
    lookup on it can produce is a wasted request and a permanent cache entry
    with a caller-chosen key.
    """
    candidate = (accession or "").strip().upper()
    if _UNIPROT_ACCESSION_RE.match(candidate):
        return candidate
    if candidate:
        logger.debug("Ignoring malformed UniProt accession %r.", accession)
    return ""


# In-process cache: uniprot_id (uppercase) → list[dict]
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

# Hard ceiling on the result cache.
#
# Keys are format-checked (see _valid_accession), so an attacker cannot mint
# arbitrary ones any more — but the space of REAL accessions with SAbDab
# entries is still thousands, each cheaply reachable by uploading a structure
# with the matching DBREF line, and this cache has no expiry. A few thousand
# populated entries is a bounded amount of memory; unbounded growth over the
# life of a worker is not.
#
# ponytail: oldest-inserted eviction, which dicts give for free by preserving
# insertion order. Deliberately NOT the hit-count ordering used by the rate
# limiter's table — evicting the wrong key there hands an attacker a quota
# reset, whereas the worst case here is recomputing a lookup. Reach for an LRU
# only if a hit-rate measurement ever says it matters.
_CACHE_MAX_ENTRIES = 2048


def _cache_put(key: str, value: list) -> None:
    """Store ``value`` under ``key``, evicting the oldest entry past the cap."""
    with _CACHE_LOCK:
        _CACHE[key] = value
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            del _CACHE[next(iter(_CACHE))]


# In-process index of the whole SAbDab summary: PDB id (uppercase, classic
# 4-character form) → list of legacy-shaped row dicts. Built once per TTL per
# worker; ~6.7 MB resident for 11,458 entries / 21,914 rows.
_SUMMARY_INDEX: Optional[dict] = None
_SUMMARY_EXPIRES_AT = 0.0
_SUMMARY_LOCK = threading.Lock()

# SAbDab publishes weekly, so a day-old index is never meaningfully stale.
_SUMMARY_TTL_SEC = 24 * 60 * 60

# A FAILED fetch gets its own, much shorter TTL, and it is the reason the two
# are separate constants. Caching a failure for the full 24 h would turn a
# two-minute upstream blip into a day of "no known binders" — which is exactly
# the shape of the bug this repoint fixes, where a dead lookup was
# indistinguishable from a target that genuinely has no antibodies. Retrying
# every 5 minutes bounds a dead upstream at ~1 request per 5 min per worker
# instead of the 41-per-analysis it used to cost.
_SUMMARY_ERROR_TTL_SEC = 5 * 60

# Columns this module reads out of the summary CSV. Checked on every parse:
# if SAbDab renames or drops one, the parse RAISES instead of quietly
# producing an index full of blanks. The previous endpoint's failure was
# silent for exactly this reason, so silence is the thing being designed out.
_SUMMARY_REQUIRED_COLUMNS = frozenset({
    "PDB", "Hchain", "Lchain", "antigen_chain", "resolution", "antigen_species",
})

# ---------------------------------------------------------------------------
# 3-letter to 1-letter amino acid code map (includes selenomethionine/cysteine)
# ---------------------------------------------------------------------------
_THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M", "SEC": "C",
}



# ---------------------------------------------------------------------------
# Automatic UniProt ID resolution
# ---------------------------------------------------------------------------

def _extract_uniprot_from_dbref(pdb_path, chain_id: str) -> str:
    """Extract UniProt accession from PDB DBREF records or mmCIF _struct_ref.

    PDB format example:
        DBREF  1HEW A    1   129  UNP    P00698   LYC_CHICK       19    147

    The wwPDB splits the record in two whenever the accession is wider
    than the 8-character field above, which is the case for every
    10-character accession. The accession then sits on the DBREF2 line;
    DBREF1 carries the database name and the entry name, never the
    accession:
        DBREF1 5YTL A    2   323  UNP                  A0A1W6VP04_GEOTD
        DBREF2 5YTL A     A0A1W6VP04                         31         352

    A plain DBREF for the chain WINS over a two-line pair. Both forms
    appear together on ~0.5% of entries, and there the pair is typically
    an expression tag or fusion partner covering a short N-terminal
    segment (21JI chain A: a 90-residue rat tag alongside the 777-residue
    protein). Preferring the plain record keeps this function's answer for
    every file that already had one, so the two-line branch can only add
    an accession where there was none.

    mmCIF format: uses BioPython MMCIF2Dict to parse _struct_ref and
    _struct_ref_seq loops. Cross-references ref_id to match chain_id
    with the correct UniProt accession.

    Args:
        pdb_path: Path to the uploaded PDB or mmCIF file.
        chain_id: Chain identifier to look up.

    Returns:
        UniProt accession string, or empty string if not found.
    """

    pdb_str = str(pdb_path)

    if pdb_str.endswith(".cif"):
        return _valid_accession(_extract_uniprot_from_cif(pdb_str, chain_id))

    # PDB format: parse DBREF lines
    try:
        with open(pdb_str, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""

    # Chain armed by a UNP-typed DBREF1, or None. Holding the chain rather
    # than a bool is what stops a DBREF1 for one chain arming a DBREF2 for
    # another; None as the empty value keeps a blank chain column (which is
    # a legal, and matchable, chain id here) from reading as "armed".
    armed_chain = None

    # First two-line accession seen for this chain. Only returned if no
    # plain DBREF matches -- see the docstring on precedence.
    two_line_accession = ""
    two_line_seen = False

    for line in text.splitlines():
        # Chain is column 12 in all three record types, so read it once,
        # above the dispatch. That makes this guard cover EVERY line, not
        # just DBREF ones -- a bare "END" is 3 characters. Keep it here:
        # moving it into the branches reintroduces an IndexError on any
        # file whose lines are not all padded to 80 columns.
        if len(line) < 13:
            continue
        dbref_chain = line[12].strip()

        if line.startswith("DBREF "):
            # wwPDB 1-based cols 13 = chain, 27-32 = db, 34-41 = accession.
            db_name = line[26:32].strip()
            accession = line[33:41].strip()
            if dbref_chain == chain_id and db_name in ("UNP", "SWS", "TRE"):
                # Validated here rather than at the call site: this is the one
                # place both the PDB and the mmCIF branch pass through, so it
                # is the only place that has to be right.
                return _valid_accession(accession)

        elif line.startswith("DBREF1"):
            # Two-line form, used whenever the accession does not fit the
            # 8-character field above -- which is every 10-character A0A...
            # accession. DBREF1 is the only half that names the database,
            # so a non-UNP pair can only be refused here.
            db_name = line[26:32].strip()
            armed_chain = (
                dbref_chain if db_name in ("UNP", "SWS", "TRE") else None
            )

        elif line.startswith("DBREF2"):
            # wwPDB 1-based cols 19-40 = accession -> line[18:40].
            # Both halves must name the wanted chain: the file is
            # caller-uploaded, so the pairing is checked, not trusted.
            if not two_line_seen and armed_chain == dbref_chain == chain_id:
                two_line_seen = True
                two_line_accession = _valid_accession(line[18:40].strip())
            # A DBREF2 consumes its DBREF1, whether or not it matched.
            armed_chain = None

    return two_line_accession


def _extract_uniprot_from_cif(cif_path: str, chain_id: str) -> str:
    """Extract UniProt accession from mmCIF _struct_ref / _struct_ref_seq loops.

    Uses BioPython MMCIF2Dict for reliable parsing of loop-format tables.
    Cross-references _struct_ref_seq.pdbx_strand_id (chain) with
    _struct_ref.db_name (UNP) and _struct_ref.pdbx_db_accession.

    Args:
        cif_path: Path to the mmCIF file.
        chain_id: Chain identifier to look up.

    Returns:
        UniProt accession string, or empty string if not found.
    """
    try:
        from Bio.PDB.MMCIF2Dict import MMCIF2Dict  # noqa: PLC0415
    except ImportError:
        logger.debug("MMCIF2Dict not available for CIF DBREF parsing.")
        return ""

    try:
        cif_dict = MMCIF2Dict(cif_path)
    except Exception:
        logger.debug("Failed to parse CIF file: %s", cif_path, exc_info=True)
        return ""

    # _struct_ref contains: id, db_name, pdbx_db_accession
    ref_ids = cif_dict.get("_struct_ref.id", [])
    ref_db_names = cif_dict.get("_struct_ref.db_name", [])
    ref_accessions = cif_dict.get("_struct_ref.pdbx_db_accession", [])

    # MMCIF2Dict returns a string instead of a list for single-value entries
    if isinstance(ref_ids, str):
        ref_ids = [ref_ids]
    if isinstance(ref_db_names, str):
        ref_db_names = [ref_db_names]
    if isinstance(ref_accessions, str):
        ref_accessions = [ref_accessions]

    # Build ref_id -> accession map for UNP entries
    unp_map = {}  # ref_id -> accession
    for i, ref_id in enumerate(ref_ids):
        if i < len(ref_db_names) and i < len(ref_accessions):
            if ref_db_names[i].upper() in ("UNP", "SWS", "TRE"):
                unp_map[ref_id] = ref_accessions[i]

    if not unp_map:
        return ""

    # _struct_ref_seq links ref_id to chain (pdbx_strand_id)
    seq_ref_ids = cif_dict.get("_struct_ref_seq.ref_id", [])
    seq_strand_ids = cif_dict.get("_struct_ref_seq.pdbx_strand_id", [])

    if isinstance(seq_ref_ids, str):
        seq_ref_ids = [seq_ref_ids]
    if isinstance(seq_strand_ids, str):
        seq_strand_ids = [seq_strand_ids]

    # Find the ref_id linked to our chain_id
    for i, strand_id in enumerate(seq_strand_ids):
        if strand_id == chain_id and i < len(seq_ref_ids):
            ref_id = seq_ref_ids[i]
            if ref_id in unp_map:
                return unp_map[ref_id]

    # If chain matching fails, return the first UNP accession found
    if unp_map:
        return next(iter(unp_map.values()))

    return ""


# Below 20 residues a whole-sequence match stops being evidence: the 10-mer
# HUMAN neurokinin B is byte-identical to marsh frog P67935, the only entry
# UniProt stores at that length. Above it a length floor is the wrong lever,
# because wrong answers reach well past 300 residues under BOTH designs: a
# reviewed-only count returns a lone wrong organism at 346 aa (Q6ZSG1, shared
# with 14 other primates) and 544 aa (Q5VTE6), and the uniqueness rule below
# still does at 337 aa -- 9P0K_D and 8GGA_B, human G-protein beta-1 answered
# as Bos taurus. No threshold reaches that class. The uniqueness rule reaches
# it only where the siblings are genuinely tied, which is why the query counts
# all of UniProtKB.
#
# 20 admits the short fully-modelled chains this function handles best: 1ACW_A
# -> P56215 at 29 aa and 1AGT_A -> P46111 at 38 aa, both returning the file's
# own DBREF accession at the entry's exact length. Neither reaches this code
# in production -- both have a resolvable DBREF and return at step 1 -- so
# they bound the function, not the path.
_MIN_SEARCHABLE_LENGTH = 20


def _search_uniprot_by_sequence(sequence: str) -> str:
    """Resolve a chain sequence to a UniProt accession by exact-sequence match.

    Asks UniProtKB for every entry whose CRC64 checksum equals this
    sequence's, and accepts the answer ONLY when exactly one exists.

    Refusing on ambiguity is the whole point. One sequence is often carried by
    several species' entries -- haemoglobin subunit beta is identical in human,
    bonobo and chimpanzee -- and nothing in the sequence chooses between them.
    Returning one anyway produced a confident wrong answer that the caller's
    identity check cannot catch, because identical sequences score 100%; for
    that case it also silently zeroed the known-binder lookup, because P68872
    indexes no PDB entries at all.

    The count is deliberately taken over ALL of UniProtKB, unreviewed and
    fragment entries included. Do NOT add ``reviewed:true`` or
    ``fragment:false`` here: those filters delete the entries that constitute
    the tie, so a sequence several organisms share comes back as a unique
    match and the wrong organism is asserted at "100.0% identity". Human VHL
    P40337 is the worked case -- five entries carry that sequence, four of them
    chimpanzee or bonobo, and the filtered query returns exactly one of them.
    ``TestLiveCapability`` checks it against the live API, but those tests are
    opt-in behind ``SCOUT_UNIPROT_LIVE=1``; what runs by default is the
    hermetic assertion on the query this function puts on the wire.

    The cost is a trade, and the measurement does not flatter it. Over 5271
    random PDB chains (163 checksum hits, 132 with DBREF ground truth) a
    filtered pick-first query answered 89 times at 15% error; this rule
    answers 59 times at 17%. Those intervals overlap, so on that population
    the uniqueness rule buys NO measurable precision -- it answers 34% less
    often at the same error rate, refusing 44 of 76 previously correct
    answers (57.9%, CI 47-68%). Two narrower frames agree on the size: 36.5%
    over random reviewed human entries, 40.5% over named therapeutic targets.

    Read that against the population it came from. Ground truth exists only
    for a chain that HAS a DBREF -- and such a chain resolves at step 1 and
    never arrives here. Of those 163 hits, 31 carried no DBREF at all, and
    those are what production actually sends to this function: models,
    designs, non-model organisms. On a frame built that way -- 240
    non-model-organism chains -- the filtered query answered 18 times with 16
    of them the WRONG ORGANISM. So the rule is protection against that
    specific failure on the traffic that gets here, NOT a general precision
    win, and this docstring should not be read as claiming one. Recovering
    the recall belongs to a narrower rule -- resolve a tie whose members all
    share one organism -- not to restoring the filters.

    Dropping the filters also ADMITS wrong answers the filters excluded: 6 of
    the 10 wrong answers in that sample had no filtered match at all, two of
    them at 337 aa. Not one-sided in either direction.

    The rule is not complete, and the gap that fires is not the obvious one. A
    length-truncated or fragmentary entry can be the unique match while every
    correct entry differs from it by a residue and so never forms a tie. The
    102-aa Xenopus histone H4 of 1KX5_B, initiator methionine excised, matches
    exactly one entry in all of UniProtKB -- a hoatzin -- because the 845
    correct H4 entries, Xenopus among them, are all one residue longer. No
    length or identity threshold closes that: neither is evidence about
    organism.

    Matching is byte-exact, so any difference misses, and most deposited
    crystal chains do: 12 of these 13 (1HEW_A, 1MBN_A, 4INS_A/B/C/D,
    1KX5_A/B/C/D, 1BPI_A, 1PGB_A, 1SHG_A), and 1387 of 1431 -- 96.9% -- in a
    random PDB sample. Signal peptides and excised initiator methionines are
    the obvious guess and explain only 2 of the 11 whose DBREF entry still
    exists; mature and domain constructs dominate (1SHG_A is a 57-aa SH3
    domain cut from a 2477-aa entry), and two more miss because their DBREF
    names a since-deleted accession. Both counts were measured before #209
    restored MSE/SEC to the extracted sequence; none of these 13 chains carries
    an MSE atom, but the 1431-chain figure predates that change and no one has
    re-run it since.

    The 13th chain is the warning, not the reassurance: 1KX5_B hits, and
    returns the hoatzin above for a Xenopus nucleosome. What this function
    recovers is therefore NOT reliably a chain at full canonical length. A
    de-novo design has no UniProt entry, so it returns "".

    ponytail: exact-match only. A similarity search (EBI ncbiblast) would
    reach the truncated chains, but it is submit/poll -- a POST to ``/run``
    then a separate ``/status/<jobid>`` -- which does not fit a blocking
    request, and it makes the organism problem strictly worse by returning
    near-matches from the species byte-exact matching excludes.

    Args:
        sequence: One-letter amino acid sequence string. Anything shorter than
            ``_MIN_SEARCHABLE_LENGTH`` returns "" without a request.

    Returns:
        UniProt accession string, or "" if there is no unambiguous match.
    """

    if not sequence or len(sequence) < _MIN_SEARCHABLE_LENGTH:
        return ""

    try:
        from Bio.SeqUtils.CheckSum import crc64  # noqa: PLC0415
    except Exception:
        # Loud on purpose: swallowing this at DEBUG would return "" for every
        # sequence forever -- the silent death this function was fixed for.
        # Broader than ImportError because a half-written install raises
        # RuntimeError/OSError. Note this does NOT keep a corrupt BioPython off
        # the request path: _extract_chain_sequence runs first and still lets
        # such an error escape.
        logger.warning(
            "Bio.SeqUtils.CheckSum.crc64 unavailable; sequence search disabled.",
            exc_info=True,
        )
        return ""

    try:
        # BioPython renders this as "CRC-<16 hex>"; UniProt wants the bare hex.
        checksum = crc64(sequence).replace("CRC-", "")
        resp = requests.get(
            UNIPROTKB_SEARCH_URL,
            params={
                # No reviewed/fragment filter on purpose -- see the docstring.
                # Filtering here hides the ties this function exists to detect.
                "query": f"(checksum:{checksum})",
                "format": "json",
                # Two rows because the header-missing fallback below counts
                # ROWS. Never lower this: at size=1 a tie returns one row, and
                # a missing header would then read as a unique match.
                "size": "2",
                "fields": "accession",
            },
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if not resp.ok:
            # Say so: a 429 or 503 is otherwise indistinguishable in the log
            # from "this protein is not in UniProt".
            logger.info(
                "UniProt sequence search returned HTTP %s; treating as no match.",
                resp.status_code,
            )
            return ""
        results = resp.json().get("results", [])

        # x-total-results is the FULL match count; len(results) is capped by
        # `size`, so it can only ever undercount a tie.
        try:
            total = int(resp.headers.get("x-total-results", ""))
        except (TypeError, ValueError):
            total = len(results)

        # len(results) too: a header saying 1 while the body carries two rows
        # is a disagreement, and trusting the header alone would accept it.
        if total != 1 or len(results) != 1:
            if total > 1:
                # "at least" because the fallback above is row-capped.
                logger.info(
                    "Sequence matches at least %d UniProt entries; "
                    "refusing to guess between them.", total,
                )
            return ""

        # Format-check for the same reason the DBREF path does: the accession
        # is interpolated into a UniProtKB URL path and becomes a _CACHE key,
        # and it arrives over the network.
        return _valid_accession(results[0].get("primaryAccession", ""))
    except Exception:
        logger.debug("UniProt sequence search failed.", exc_info=True)

    return ""


def _fetch_uniprot_metadata(uniprot_id: str) -> dict:
    """Fetch protein name and sequence from UniProt for validation.

    Args:
        uniprot_id: UniProt accession (e.g. "P00698").

    Returns:
        Dict with keys 'protein_name' (str) and 'sequence' (str).
        Both are empty strings on failure.
    """
    result = {"protein_name": "", "sequence": ""}

    try:
        # Fetch JSON metadata for protein name.
        resp = requests.get(
            f"https://rest.uniprot.org/uniprotkb/{uniprot_id.upper()}",
            params={"format": "json", "fields": "protein_name,organism_name"},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if resp.ok:
            data = resp.json()
            # Extract recommended name or first submitted name.
            prot = data.get("proteinDescription", {})
            rec = prot.get("recommendedName", {})
            # The live API spells this "submissionNames"; "submittedNames" was
            # the older spelling, kept as a harmless second try. Reading only
            # the old one returned an empty name for every submission-named
            # entry -- unreachable while the sequence search filtered to
            # reviewed entries, reachable now that it counts all of UniProtKB.
            submitted = (
                prot.get("submissionNames") or prot.get("submittedNames") or []
            )
            if rec:
                name = rec.get("fullName", {}).get("value", "")
            elif submitted:
                name = submitted[0].get("fullName", {}).get("value", "")
            else:
                name = ""
            # A fragment entry is a partial sequence record, so say so: an
            # exact checksum match against one is still a match, but
            # annotating a whole chain with it is not the same claim. Test the
            # VALUE -- "flag" is also present on complete entries, where it
            # reads "Precursor" (P00698 does).
            if prot.get("flag") == "Fragment":
                name = f"{name} (fragment)" if name else "(fragment)"
            # Organism is appended even when the name is empty. Gating it on
            # the name dropped both for a submission-named entry, leaving a
            # bare accession on screen beside "identity: 100.0%".
            org = data.get("organism", {}).get("scientificName", "")
            if org:
                name = f"{name} ({org})" if name else org
            result["protein_name"] = name
    except Exception:
        logger.debug("UniProt metadata fetch failed for %s.", uniprot_id, exc_info=True)

    # Fetch FASTA sequence.
    result["sequence"] = _fetch_uniprot_sequence(uniprot_id)

    return result


# Minimum sequence identity required to accept a resolved UniProt accession.
# PDB structures may be truncated domains or contain mutations, so 70% is
# permissive enough for legitimate constructs while rejecting wrong proteins.
_MIN_VALIDATION_IDENTITY = 0.70


def resolve_uniprot_id(pdb_path, chain_id: str) -> dict:
    """Automatically determine and validate the UniProt accession for a PDB chain.

    Resolution strategy:
        1. Extract accession from PDB DBREF records (instant, authoritative
           for RCSB structures). If found, validate by sequence identity.
           If validation API is unreachable, accept the DBREF accession
           anyway (DBREF is depositor-annotated and highly reliable).
        2. Fall back to an EXACT sequence match when step 1 produced no
           accepted accession -- no reference record, an unreadable one, or one
           that failed validation -- provided a chain sequence was extractable
           and the match differs from the accession step 1 already rejected.
           AlphaFold DB downloads reach step 2 more often than their DBREF
           line suggests: ``_extract_uniprot_from_dbref`` reads a fixed 8-char
           column per the PDB spec, so a 10-char accession such as A0A2K5QDT7
           truncates to A0A2K5QD and is rejected at step 1. Those are
           overwhelmingly TrEMBL, so this path sees unreviewed entries far out
           of proportion to their share of UniProt. Accepted only when the
           sequence matches exactly one UniProt entry; see
           ``_search_uniprot_by_sequence``.
           The identity gate below cannot screen this path: a checksum match is
           byte-equal to the entry's canonical sequence, so it scores 1.0 by
           construction and ``must_validate`` only proves UniProt answered.
           Uniqueness is the sole correctness check on a step-2 result.

    Args:
        pdb_path: Path to the uploaded PDB or mmCIF file.
        chain_id: Chain identifier selected by the user.

    Returns:
        Dict with keys:
            uniprot_id    str   — validated accession, or "" if none confirmed
            protein_name  str   — UniProt protein name + organism, or ""
            identity      float — sequence identity (0–1), or None
            identity_pct  str   — formatted identity e.g. "93.2%", or "unknown"
            source        str   — "dbref", "sequence_search", or ""
    """
    empty_result = {
        "uniprot_id": "",
        "protein_name": "",
        "identity": None,
        "identity_pct": "unknown",
        "source": "",
    }

    # Extract chain sequence once — needed for validation in all paths.
    _, chain_seq = _extract_chain_sequence(pdb_path, chain_id)

    def _validate_and_build(accession: str, source: str, must_validate: bool) -> dict:
        """Fetch UniProt metadata and optionally validate by sequence identity.

        For DBREF-sourced accessions (must_validate=False), accept even if the
        UniProt API is unreachable — DBREF is depositor-annotated and reliable.
        For sequence-search results (must_validate=True), reject if identity
        is below threshold or API fails.
        """
        meta = _fetch_uniprot_metadata(accession)

        # If we got the sequence, validate identity.
        if meta["sequence"] and chain_seq:
            identity = _sequence_identity(meta["sequence"], chain_seq)
            identity_pct = f"{identity * 100:.1f}%"

            if identity < _MIN_VALIDATION_IDENTITY:
                logger.info(
                    "Rejected UniProt %s for chain %s: identity %s < 70%% threshold.",
                    accession, chain_id, identity_pct,
                )
                return empty_result

            logger.info(
                "Confirmed UniProt %s (%s) for chain %s, identity %s, source: %s.",
                accession, meta["protein_name"], chain_id, identity_pct, source,
            )
            return {
                "uniprot_id": accession,
                "protein_name": meta["protein_name"],
                "identity": identity,
                "identity_pct": identity_pct,
                "source": source,
            }

        # Could not fetch UniProt sequence — API may be down or timed out.
        if must_validate:
            logger.info("Cannot validate %s (no UniProt sequence) — rejecting.", accession)
            return empty_result

        # DBREF source: accept without sequence validation. DBREF is
        # depositor-annotated and authoritative for RCSB structures.
        logger.info(
            "Accepting DBREF UniProt %s for chain %s without sequence validation "
            "(UniProt API unavailable). Protein name: %s",
            accession, chain_id, meta["protein_name"] or "unknown",
        )
        return {
            "uniprot_id": accession,
            "protein_name": meta["protein_name"],
            "identity": None,
            "identity_pct": "unknown",
            "source": source,
        }

    # Step 1: Try DBREF extraction (instant, no network calls).
    dbref_accession = _extract_uniprot_from_dbref(pdb_path, chain_id)
    if dbref_accession:
        result = _validate_and_build(dbref_accession, "dbref", must_validate=False)
        if result["uniprot_id"]:
            return result

    # Step 2: Fall back to sequence-based search (requires chain sequence).
    if chain_seq:
        search_accession = _search_uniprot_by_sequence(chain_seq)
        if search_accession and search_accession != dbref_accession:
            result = _validate_and_build(search_accession, "sequence_search", must_validate=True)
            if result["uniprot_id"]:
                return result

    logger.info("Could not resolve UniProt ID for chain %s in %s.", chain_id, pdb_path)
    return empty_result


# ---------------------------------------------------------------------------
# Binder classification
# ---------------------------------------------------------------------------

def _classify_binder(h_chain: Optional[str], l_chain: Optional[str]) -> str:
    """Classify binder type from heavy/light chain configuration.

    SAbDab records VHH/nanobodies as entries with a heavy chain but no
    light chain. IgG and Fab fragments have both heavy and light chains.

    Args:
        h_chain: Heavy chain ID, or None/empty string if absent.
        l_chain: Light chain ID, or None/empty string if absent.

    Returns:
        str: Human-readable binder type label.
    """
    has_h = bool(h_chain and h_chain.strip() and h_chain.lower() != "na")
    has_l = bool(l_chain and l_chain.strip() and l_chain.lower() != "na")
    if has_h and not has_l:
        return "VHH/Nanobody"
    if has_h and has_l:
        return "IgG/Fab"
    return "Unknown"


# ---------------------------------------------------------------------------
# Contact residue computation
# ---------------------------------------------------------------------------

def _compute_contacts(
    pdb_text: str,
    antigen_chain: str,
    ab_chains: list,
    cutoff: float = _CONTACT_CUTOFF_ANGSTROM,
) -> Optional[list]:
    """Compute antigen residue numbers in contact with antibody chains.

    Uses BioPython to parse the structure and NumPy for vectorised distance
    computation.

    Returns ``None`` when the coordinates could not be read at all -- the
    libraries are missing, or anything in the parse-and-measure raised --
    and a list otherwise. The caller caches what it gets forever, so those
    two have to stay apart: an unreadable body that answers ``[]`` is
    indistinguishable from an interface with nothing in it, and gets
    written down as a fact.

    A structure that parsed but does not contain the named chains answers
    ``[]``: re-downloading produces the same absence every time, so there is
    nothing to retry. That is right when the chains really are absent and
    WRONG in one case this module does not handle -- SAbDab's chain ids come
    from mmCIF ``auth_asym_id`` and the download URL serves the legacy .pdb,
    which can label chains differently. There is no chain-id mapping anywhere
    here, so such an entry caches a permanent "contacts nothing". Pre-existing,
    and a retry would not fix it; a mapping would.

    Args:
        pdb_text: Raw text content of a PDB coordinate file.
        antigen_chain: Chain ID of the antigen in the PDB file.
        ab_chains: List of antibody chain IDs (heavy and/or light).
        cutoff: Distance threshold in Angstroms.

    Returns:
        Sorted list of antigen residue sequence numbers (PDB auth_seq_id), or
        ``None`` if the structure could not be read.
    """
    try:
        from Bio.PDB import PDBParser  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        logger.debug("BioPython or NumPy not available; skipping contact computation.")
        return None

    try:
        parser = PDBParser(QUIET=True)
        structure = parser.get_structure("s", StringIO(pdb_text))
        model = next(structure.get_models())
        chain_map = {chain.id: chain for chain in model.get_chains()}

        if antigen_chain not in chain_map:
            logger.debug(
                "Antigen chain %s not found in structure (available: %s)",
                antigen_chain,
                list(chain_map.keys()),
            )
            return []

        # Collect all antibody heavy atom coordinates.
        ab_atom_coords = []
        for ab_chain_id in ab_chains:
            if ab_chain_id in chain_map:
                for residue in polymer.polymer_residues(chain_map[ab_chain_id]):
                    for atom in residue.get_atoms():
                        ab_atom_coords.append(atom.coord)

        if not ab_atom_coords:
            return []

        ab_coords = np.array(ab_atom_coords, dtype=np.float32)

        # For each standard antigen residue, find the minimum distance to
        # any antibody atom. Flag residues within the cutoff.
        contact_residues = []
        for residue in polymer.polymer_residues(chain_map[antigen_chain]):
            res_coords = np.array(
                [atom.coord for atom in residue.get_atoms()], dtype=np.float32
            )
            if len(res_coords) == 0:
                continue
            # Vectorised pairwise minimum distance.
            diffs = res_coords[:, np.newaxis, :] - ab_coords[np.newaxis, :, :]
            min_dist = float(np.sqrt((diffs ** 2).sum(axis=2)).min())
            if min_dist <= cutoff:
                contact_residues.append(residue.id[1])

        return sorted(contact_residues)

    except Exception:
        logger.debug("Contact computation error.", exc_info=True)
        return None


# Bound on a dead files.rcsb.org, mirroring _RCSB_RETRY_AT for the search host.
# Refusing to cache a failure is what stops the permanent zero, but it also
# means every analysis retries, and a retry is up to _MAX_CONTACT_STRUCTURES
# downloads (~0.5-5 MB each, see that constant) started inside
# anon_compute_slot(ANON_MAX_CONCURRENT_RUNS), which is 2.
#
# ponytail: one SHARED unlocked timestamp, matching _RCSB_RETRY_AT, and armed
# ONLY by a TRANSPORT failure -- a non-404 error status or a raise from
# requests. That restriction is the whole of what makes "shared" tolerable.
# The per-entry failures are the ones a structure carries forever: a 404
# (mmCIF-only entry) and a body that will not parse. Neither arms this, so no
# single bad structure can darken contact downloads for every other target.
# A transport error is not entry-specific in that way -- it says the host is
# unhappy -- so it does darken them, for one TTL, and an accession analysed in
# that window gets no interface rather than a wrong one. Blunt, and the same
# trade _RCSB_RETRY_AT already makes one layer up.
#
# Note this is a deadline, not a flag: a later success does not clear it, only
# the clock does. Deliberate, and again the same as _RCSB_RETRY_AT -- clearing
# on success would let one healthy entry in a round re-open the floodgate for
# the four still failing behind it.
_PDB_FILE_RETRY_AT = 0.0


def _fetch_and_compute_contacts(
    pdb_id: str, antigen_chain: str, ab_chains: list
) -> Optional[list]:
    """Download a PDB file from RCSB and compute interface contact residues.

    Args:
        pdb_id: RCSB PDB accession (case-insensitive).
        antigen_chain: Antigen chain ID in the PDB file.
        ab_chains: Antibody chain IDs.

    Returns:
        Sorted list of contact residue numbers; ``[]`` when the answer is
        genuinely no contacts (or there is nothing to compute against, or the
        entry has no PDB-format file at all); ``None`` when the coordinates
        could not be read and the question is still open.

        ``fetch_known_binders`` caches what this returns for the life of the
        worker, so ``[]`` for a 503 is what pins "this antibody touches
        nothing" long after files.rcsb.org recovers -- the same
        silent-permanent-zero shape as the search-side outage above, one layer
        down. Only this layer sees the status code, so only this layer can tell
        the two apart.

        A 404 is deliberately on the ``[]`` side. Structures too large for the
        legacy format are mmCIF-only and answer 404 forever, so classifying
        that as an outage would re-download the same absence on every retry.

        An armed backoff also answers ``None``, without asking the host at
        all. Same meaning -- the question is open -- and the same handling:
        the entry stays pending and is re-attempted once the TTL lapses.
    """
    global _PDB_FILE_RETRY_AT
    if not antigen_chain or not ab_chains:
        return []
    if time.monotonic() < _PDB_FILE_RETRY_AT:
        return None
    try:
        url = RCSB_PDB_DOWNLOAD_URL.format(pdb_id=pdb_id.upper())
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT_SEC)
        if resp.status_code == 404:
            logger.debug("No PDB-format file for %s (404).", pdb_id)
            return []
        if not resp.ok:
            logger.warning(
                "PDB download failed for %s: HTTP %s. Backing off "
                "files.rcsb.org for %ss.",
                pdb_id, resp.status_code, _RCSB_ERROR_TTL_SEC,
            )
            _PDB_FILE_RETRY_AT = time.monotonic() + _RCSB_ERROR_TTL_SEC
            return None
        # A parse failure below returns None WITHOUT arming the backoff. The
        # bytes arrived, so the host is fine; the entry is re-downloaded on the
        # next lookup of this accession and nothing else is affected. That is
        # one download per lookup per unreadable structure -- bounded by
        # _MAX_CONTACT_STRUCTURES, and the price of not writing down a zero we
        # cannot justify.
        return _compute_contacts(resp.text, antigen_chain, ab_chains)
    except Exception:
        logger.warning(
            "PDB download error for %s. Backing off files.rcsb.org for %ss.",
            pdb_id, _RCSB_ERROR_TTL_SEC, exc_info=True,
        )
        _PDB_FILE_RETRY_AT = time.monotonic() + _RCSB_ERROR_TTL_SEC
        return None


# ---------------------------------------------------------------------------
# SAbDab query
# ---------------------------------------------------------------------------

# A dead RCSB is bounded the same way a dead SAbDab is, and for the same
# reason. Refusing to cache an outage is what stops the permanent zero, but it
# also means every analysis re-probes — and this probe is a 12-second request
# made INSIDE anon_compute_slot(ANON_MAX_CONCURRENT_RUNS), which is 2. An RCSB
# that hangs rather than refusing would hold both slots and turn concurrent
# /analyze calls into 503 BUSY, with no cache write left to damp it. One
# timestamp bounds that at a single probe per TTL per worker.
#
# ponytail: one SHARED timestamp, not per-accession, and an unlocked float.
# Shared means any failure darkens the lookup for every accession until the TTL
# lapses — blunt, but the failures this actually sees are upstream-wide, and
# the degraded answer is the safe one ([] returned, nothing cached). Unlocked
# because this only needs a bound; the summary's lock buys single-flight on a
# megabyte download, which this has no equivalent of, so a race at the boundary
# costs at most a couple of extra probes. Go per-accession only if a one-off
# malformed response for a single target is ever seen tripping it.
_RCSB_ERROR_TTL_SEC = 5 * 60
_RCSB_RETRY_AT = 0.0


def _rcsb_pdb_ids_for_uniprot(
    uniprot_id: str, limit: int = _RCSB_PROBE_LIMIT
) -> Optional[list]:
    """Return PDB IDs from RCSB that contain a polymer entity with the given
    UniProt accession, sorted by RCSB relevance score (best structures first).

    Args:
        uniprot_id: UniProt accession in uppercase (e.g. "P00533").
        limit: Maximum number of PDB IDs to return.

    Returns:
        List of uppercase PDB ID strings; ``[]`` when RCSB answered and matched
        nothing, ``None`` when RCSB could not be read.

        The caller needs those two apart. ``fetch_known_binders`` writes an
        unexpiring "no known binders" for this accession on the strength of an
        empty answer, so returning ``[]`` for an outage would leave that miss
        standing long after the upstream recovered — the same
        silent-permanent-zero shape as the retired-endpoint bug this module
        already carries guards for. Only this layer sees the status code and
        the exception, so only this layer can tell them apart.
    """
    global _RCSB_RETRY_AT
    if time.monotonic() < _RCSB_RETRY_AT:
        return None

    query_payload = {
        "query": {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": (
                    "rcsb_polymer_entity_container_identifiers"
                    ".reference_sequence_identifiers.database_accession"
                ),
                "operator": "exact_match",
                "value": uniprot_id,
            },
        },
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": limit},
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }
    try:
        resp = requests.post(
            RCSB_SEARCH_URL,
            json=query_payload,
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if not resp.ok:
            logger.warning("RCSB search returned HTTP %s for %s", resp.status_code, uniprot_id)
            _RCSB_RETRY_AT = time.monotonic() + _RCSB_ERROR_TTL_SEC
            return None
        # A query that matches nothing comes back 204 with a ZERO-BYTE body,
        # which json() below raises on. Without this branch that raise would be
        # classified as an outage — and because the backoff above is SHARED
        # across accessions, one target that legitimately has no structures
        # would darken the known-binder lookup for every other target for a
        # full error TTL. Verified against the live API 2026-09-02: five
        # well-formed accessions with no PDB entry all answered 204/0 bytes,
        # and `resp.ok` is True for 204, so the check above does not cover it.
        if resp.status_code == 204:
            return []
        data = resp.json()
        # ``data["result_set"]``, not ``.get(..., [])``. A 2xx whose body is not
        # the documented shape is an unreadable answer, not a zero; defaulting
        # it to [] mints a forgeable genuine-zero and gets it cached. The
        # KeyError falls into the handler below, where it belongs.
        return [hit["identifier"].upper() for hit in data["result_set"]]
    except Exception:
        logger.warning("RCSB search failed for %s.", uniprot_id, exc_info=True)
        _RCSB_RETRY_AT = time.monotonic() + _RCSB_ERROR_TTL_SEC
        return None


def _classic_pdb_id(extended_id: str) -> str:
    """Reduce SAbDab's extended PDB identifier to the classic 4-character one.

    SAbDab 2 reports PDB entries in the extended ``pdb_0000XXXX`` form, where
    ``XXXX`` is the classic accession. RCSB's search API — and the coordinate
    download URL — still speak the classic form, so the index is keyed on it.
    Verified against the live summary: all 21,914 rows match ``pdb_0000`` plus
    four characters.

    Returns "" for anything that is not in that shape, so a future format
    change cannot silently produce garbage keys.
    """
    value = (extended_id or "").strip()
    if len(value) == 12 and value.lower().startswith("pdb_0000"):
        return value[-4:].upper()
    return ""


def _parse_summary_csv(text: str) -> dict:
    """Index SAbDab's whole-database summary CSV by classic PDB id.

    Rows come out shaped like the legacy per-structure TSV rows so that
    ``query_sabdab`` can read them unchanged.

    Raises:
        ValueError: if the payload is not the summary CSV — a required column
            is missing, or no row carries a usable PDB id. This is deliberate.
            The retired endpoint failed by returning an HTML page that parsed
            into zero rows, which the old code could not tell apart from "this
            protein has no known antibodies", so the feature was dead for
            months without a single error. Anything that is not recognisably
            the summary must be loud.
    """
    reader = csv.DictReader(StringIO(text))
    missing = _SUMMARY_REQUIRED_COLUMNS.difference(reader.fieldnames or ())
    if missing:
        raise ValueError(
            f"SAbDab summary is missing expected column(s): {sorted(missing)}"
        )

    index: dict = {}
    for row in reader:
        pdb_id = _classic_pdb_id(row.get("PDB", ""))
        if not pdb_id:
            continue
        # SAbDab writes the literal "NA" for absent values, and pipe-separates
        # multi-chain antigens ("I|J"). _compute_contacts scores ONE antigen
        # chain, so take the first; the rest of that interface is not lost,
        # it simply is not the chain whose contacts get reported.
        antigen_chain = (row.get("antigen_chain") or "").split("|")[0].strip()
        species = (row.get("antigen_species") or "").strip()
        index.setdefault(pdb_id, []).append({
            "pdb": pdb_id,
            "Hchain": (row.get("Hchain") or "").strip(),
            "Lchain": (row.get("Lchain") or "").strip(),
            "antigen_chain": "" if antigen_chain.upper() == "NA" else antigen_chain,
            "resolution": (row.get("resolution") or "").strip(),
            "antigen_species": "" if species.upper() == "NA" else species,
        })

    if not index:
        raise ValueError("SAbDab summary parsed to zero usable rows")
    return index


def _sabdab_summary_index() -> dict:
    """Return the SAbDab summary index, fetching it at most once per TTL.

    The fetch happens while holding ``_SUMMARY_LOCK`` so that N concurrent
    requests on a cold worker produce ONE download rather than N. That blocks
    the other callers for the duration, which is the intended trade: they
    would otherwise each pull the same megabyte. The GIL is released across
    the socket read, so waiting threads cost no CPU.

    Never raises — a lookup failure degrades to "no known binders", the same
    as a target with none. Returns a previously built index in preference to
    an empty one when a refresh fails.
    """
    global _SUMMARY_INDEX, _SUMMARY_EXPIRES_AT
    with _SUMMARY_LOCK:
        # Deliberately keyed on the expiry alone, NOT on "do we have an
        # index". Gating this on ``_SUMMARY_INDEX is not None`` looks
        # equivalent and is not: a worker that has never succeeded would fall
        # through to the fetch on EVERY call, so a dead upstream would cost
        # one timeout per analysis instead of one per error TTL — the failure
        # this backoff exists to prevent, on the exact path where it matters
        # most. ``_SUMMARY_EXPIRES_AT`` starts at 0.0, which is below any
        # monotonic reading, so the first call still fetches.
        if time.monotonic() < _SUMMARY_EXPIRES_AT:
            return _SUMMARY_INDEX if _SUMMARY_INDEX is not None else {}
        try:
            resp = requests.get(SABDAB_SUMMARY_URL, timeout=_SUMMARY_TIMEOUT_SEC)
            resp.raise_for_status()
            index = _parse_summary_csv(resp.text)
        except Exception:
            logger.warning(
                "SAbDab summary fetch/parse failed; known-binder lookup is "
                "degraded. Retrying in %ss.", _SUMMARY_ERROR_TTL_SEC,
                exc_info=True,
            )
            _SUMMARY_EXPIRES_AT = time.monotonic() + _SUMMARY_ERROR_TTL_SEC
            # Serve the last good index if there is one. Stale beats empty:
            # SAbDab grows by a few structures a week.
            return _SUMMARY_INDEX if _SUMMARY_INDEX is not None else {}

        logger.info(
            "SAbDab summary indexed: %d PDB entries.", len(index)
        )
        _SUMMARY_INDEX = index
        _SUMMARY_EXPIRES_AT = time.monotonic() + _SUMMARY_TTL_SEC
        return _SUMMARY_INDEX


def _reset_summary_cache() -> None:
    """Drop the cached index. Test helper; not used by request handling."""
    global _SUMMARY_INDEX, _SUMMARY_EXPIRES_AT
    with _SUMMARY_LOCK:
        _SUMMARY_INDEX = None
        _SUMMARY_EXPIRES_AT = 0.0


def query_sabdab(uniprot_id: str) -> Optional[list]:
    """Find antibody/nanobody structures for a target protein via RCSB + SAbDab.

    Step 1: Query RCSB search API to get PDB IDs containing the UniProt
    accession. Step 2: look each one up in the cached SAbDab summary index to
    filter for antibody-antigen complexes.

    Step 2 used to be one HTTPS request per PDB id, each on its own raw
    ``threading.Thread`` — 40 threads and 41 requests per anonymous analysis,
    measured at ~1.9 CPU-seconds and a peak of 42 live threads. It is now a
    dict lookup against an index fetched at most once a day per worker. That
    matters beyond its own cost: it is what makes a threaded worker class
    safe, since the old fan-out multiplied by every concurrent request.

    Args:
        uniprot_id: UniProt accession (e.g. "P00533" for EGFR).

    Returns:
        List of binder dicts sorted by resolution (best first); ``None`` when
        RCSB could not be read, so nothing was ever established; ``[]``
        otherwise.

        ``[]`` is NOT by itself a genuine zero. It is also what an unreadable
        SAbDab produces, because a missing index yields no rows for any PDB id.
        So a caller that persists a miss has to check BOTH halves — ``None``
        for RCSB, and ``_sabdab_summary_index()`` for SAbDab — which is what
        ``fetch_known_binders`` does. Checking ``None`` alone is not enough.
    """
    pdb_ids = _rcsb_pdb_ids_for_uniprot(uniprot_id)
    if pdb_ids is None:
        return None
    if not pdb_ids:
        return []

    index = _sabdab_summary_index()
    all_rows: list = []
    for pdb_id in pdb_ids:
        all_rows.extend(index.get(pdb_id.upper(), ()))

    if not all_rows:
        return []

    results = []
    for entry in all_rows:
        pdb_id = (entry.get("pdb") or "").upper()
        if not pdb_id:
            continue

        h_chain = entry.get("Hchain") or ""
        l_chain = entry.get("Lchain") or ""
        antigen_chain = entry.get("antigen_chain") or ""
        resolution = entry.get("resolution")
        species = entry.get("antigen_species") or entry.get("organism") or ""
        affinity = entry.get("affinity") or ""

        # Normalise resolution to float or None.
        try:
            resolution = float(resolution) if resolution not in (None, "", "NA", "None") else None
        except (TypeError, ValueError):
            resolution = None

        binder_type = _classify_binder(h_chain or None, l_chain or None)
        ab_chains = [c.strip() for c in [h_chain, l_chain] if c and c.strip() and c.lower() != "na"]

        results.append({
            "pdb_id": pdb_id,
            "antigen_chain": antigen_chain,
            "ab_chains": ab_chains,
            "binder_type": binder_type,
            "resolution": resolution,
            "species": species,
            "affinity": str(affinity) if affinity else "",
            # No "contact_residues" key. SAbDab does not carry the interface;
            # it is computed from coordinates by fetch_known_binders, and a
            # placeholder [] here is indistinguishable downstream from a
            # computed empty interface. Absence is what says "not established".
        })

    # Deduplicate by pdb_id (keep best resolution row per structure).
    seen: dict = {}
    for r in results:
        pid = r["pdb_id"]
        if pid not in seen:
            seen[pid] = r
        else:
            existing_res = seen[pid]["resolution"]
            new_res = r["resolution"]
            if existing_res is None or (new_res is not None and new_res < existing_res):
                seen[pid] = r

    deduped = list(seen.values())
    deduped.sort(key=lambda x: (x["resolution"] is None, x["resolution"] or 99.0))
    return deduped


# ---------------------------------------------------------------------------
# Contact resolution (the per-entry half of the cache)
# ---------------------------------------------------------------------------

# How long to wait on the contact-download threads before giving up on this
# round. Was an inline literal; named so the late-thread guard in
# tests/test_scout_epitope_db_sabdab.py can shorten it instead of sleeping.
_CONTACT_JOIN_TIMEOUT_SEC = 20


def _resolve_contacts(cache_key: str, entries: list, limit: int) -> list:
    """Fill in ``contact_residues`` for the first *limit* entries lacking it.

    Returns a fresh list of fresh dicts and stores it under *cache_key*. Both
    the fresh lookup and the cache hit come through here, so an entry whose
    interface could not be computed is retried on the next lookup instead of
    being served as a permanent zero -- which is the whole point: ``_CACHE``
    has no expiry, so a 503 from files.rcsb.org during the first analysis of a
    target used to pin ``contact_residues: []`` on that binder for the life of
    the worker. The known-binder table then showed that antibody stuck on
    "pending" forever, and _get_binder_overlaps dropped it from the feasibility
    overlap silently.

    The expensive half -- the RCSB search and the SAbDab index lookup that
    produced *entries* -- is NOT discarded when a coordinate download flakes.
    Only the entries that failed are re-attempted.

    Two costs of that, both deliberate:

    A thread that outruns the join has its result DISCARDED, where the old code
    let the late write land (unsafely -- it landed in an already-cached,
    already-returned dict). So a slow-but-working structure now costs its work
    and stays pending. Bounded by re-attempting on the next lookup.

    ponytail: two concurrent callers for the same accession both resolve and
    both _cache_put, so the loser's interface is clobbered and re-fetched later.
    Harmless under the sync workers this runs on today (one request per process
    at a time); if gunicorn.conf.py's documented gthread flip ever happens,
    hold _CACHE_LOCK across the read-resolve-write instead.
    """
    pending = [
        i for i, entry in enumerate(entries[:limit])
        if "contact_residues" not in entry
    ]
    resolved: dict = {}

    if pending and time.monotonic() >= _PDB_FILE_RETRY_AT:
        # Slots, not the entry dicts. A thread that outruns the join below is
        # still running when this function caches its result, so it must not
        # hold a reference to anything reachable from _CACHE: the old worker
        # wrote straight into a dict that was already cached AND already
        # returned, mutating it under a caller mid-json.dump. A late write into
        # this local list is discarded instead, and the entry stays pending.
        slots: list = [None] * len(pending)

        def _worker(slot: int, idx: int) -> None:
            entry = entries[idx]
            slots[slot] = _fetch_and_compute_contacts(
                entry["pdb_id"], entry["antigen_chain"], entry["ab_chains"]
            )

        threads = [
            threading.Thread(target=_worker, args=(slot, idx), daemon=True)
            for slot, idx in enumerate(pending)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_CONTACT_JOIN_TIMEOUT_SEC)

        resolved = {
            idx: slots[slot] for slot, idx in enumerate(pending)
            if slots[slot] is not None
        }

    out = [dict(entry) for entry in entries]
    for idx, residues in resolved.items():
        out[idx]["contact_residues"] = residues
    _cache_put(cache_key, out)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_known_binders(uniprot_id: str, max_contact_structures: int = _MAX_CONTACT_STRUCTURES) -> list:
    """Return known antibody/nanobody binders for a protein from PDB/SAbDab.

    Queries SAbDab for all deposited structures with the target as antigen.
    For the top `max_contact_structures` entries (by resolution), downloads
    the PDB coordinate file and computes contact residues in parallel threads.
    Results are cached in memory for the process lifetime.

    Args:
        uniprot_id: UniProt accession (e.g. "P00533" for EGFR).
        max_contact_structures: Number of structures to compute contacts for.
            Remaining hits are returned without a ``contact_residues`` key.

    Returns:
        List of binder dicts. Empty when the accession is blank or malformed,
        when the target genuinely has no antibody complexes, OR when an
        upstream was unreadable. Those three are not distinguishable from the
        return value — only from the warnings the helpers log.

        An entry carries ``contact_residues`` only once the interface has
        actually been computed; see the module docstring. The key is filled in
        by a later call if the coordinate host was down for this one, so the
        returned list is a snapshot, not a stable identity — callers get a
        fresh copy every time and must not hold onto it.
    """
    # Re-checked here, not just at extraction, because this is the function
    # that mints a cache key and it is reachable from more than one resolver.
    cache_key = _valid_accession(uniprot_id)
    if not cache_key:
        return []

    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)

    if cached is None:
        sabdab_hits = query_sabdab(cache_key)
        if not sabdab_hits:
            # Only remember "no binders" when both upstreams actually
            # answered. This cache has no expiry, so a miss written during an
            # outage outlives the outage — bounded only by the entry cap
            # eventually evicting it, never by the upstream recovering. That
            # reproduces in miniature the silent-permanent-zero failure this
            # repoint fixes.
            #
            # The lookup rides on two: RCSB names the candidate structures and
            # SAbDab says which are antibody complexes. Before this guard grew
            # its RCSB arm, either being down produced the same empty list as a
            # target that genuinely has none, and vouching for SAbDab alone let
            # an RCSB outage write permanent zeroes. ``None`` is RCSB's "I
            # could not answer"; ``[]`` is an answer of zero.
            #
            # The SAbDab arm deliberately accepts a stale-but-served index (see
            # _sabdab_summary_index): the summary is published weekly, so last
            # week's copy is a fact, not an outage.
            #
            # A coordinate download that fails is NOT handled here: it costs
            # one entry's interface, not the whole (expensive) list, so
            # _resolve_contacts keeps the list and retries that entry.
            if sabdab_hits is not None and _sabdab_summary_index():
                _cache_put(cache_key, [])
            return []
        cached = sabdab_hits

    # Both paths land here, so a cache hit retries any interface that was never
    # established rather than serving it as a permanent zero.
    #
    # deepcopy, not dict(entry): a shallow copy still shares the
    # contact_residues and ab_chains LIST objects with the cache, so "the
    # caller cannot corrupt the cache" would have been true only of rebinding a
    # key and false of the mutation anyone would actually reach for
    # (list.append). The lists are small and there are at most
    # _RCSB_PROBE_LIMIT of them, against an HTTP call.
    return copy.deepcopy(
        _resolve_contacts(cache_key, cached, max_contact_structures)
    )


def cached_binders(uniprot_id: str) -> Optional[list]:
    """Binders for *uniprot_id* if this worker already has them, else ``None``.

    A read of ``_CACHE`` and nothing else: no RCSB search, no SAbDab summary,
    no coordinate downloads, no threads. It exists so a caller outside
    ``anon_compute_slot`` can benefit from an interface this worker has since
    repaired without putting a multi-upstream network call on its path --
    ``fetch_known_binders`` on a cold worker is a 12 s search plus a 60 s
    summary fetch plus a round of downloads, which is not something to hang off
    a page render.

    ``None`` means "this worker has not looked this accession up", NOT "no
    binders" -- an accession with none genuinely caches ``[]``.
    """
    key = _valid_accession(uniprot_id)
    if not key:
        return None
    with _CACHE_LOCK:
        entries = _CACHE.get(key)
    return None if entries is None else copy.deepcopy(entries)


# ---------------------------------------------------------------------------
# Sequence identity check
# ---------------------------------------------------------------------------

def _fetch_uniprot_sequence(uniprot_id: str) -> str:
    """Fetch the canonical one-letter sequence for a UniProt accession.

    Args:
        uniprot_id: UniProt accession (e.g. "P00533").

    Returns:
        One-letter amino acid sequence string, or empty string on failure.
    """
    url = UNIPROT_FASTA_URL.format(uniprot_id=uniprot_id.upper())
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT_SEC)
        if not resp.ok:
            logger.debug("UniProt FASTA request failed for %s: HTTP %s", uniprot_id, resp.status_code)
            return ""
        lines = resp.text.strip().splitlines()
        return "".join(line.strip() for line in lines if not line.startswith(">"))
    except Exception:
        logger.debug("UniProt FASTA fetch error for %s.", uniprot_id, exc_info=True)
        return ""


def _extract_chain_sequence(pdb_path, chain_id: str) -> tuple:
    """Extract the one-letter sequence and auth residue numbers from a PDB chain.

    Args:
        pdb_path: Path (str or Path) to a .pdb or .cif structure file.
        chain_id: Chain identifier to extract.

    Returns:
        Tuple (residue_numbers: list[int], sequence: str). Both are empty on
        failure (missing chain, unparseable file, BioPython not available).
    """
    try:
        from Bio.PDB import MMCIFParser, PDBParser  # noqa: PLC0415
    except ImportError:
        return [], ""

    try:
        pdb_str = str(pdb_path)
        if pdb_str.endswith(".cif"):
            parser = MMCIFParser(QUIET=True)
        else:
            parser = PDBParser(PERMISSIVE=True, QUIET=True)

        structure = parser.get_structure("query", pdb_str)
        model = next(structure.get_models())
        chain_map = {chain.id: chain for chain in model.get_chains()}

        if chain_id not in chain_map:
            logger.debug("Chain %s not found in structure.", chain_id)
            return [], ""

        residue_numbers = []
        one_letter = []
        # polymer_residues, not a bare hetflag test: MSE/SEC are ordinary
        # polymer residues deposited as HETATM, and _THREE_TO_ONE below already
        # maps both to M/C, so a hetflag gate here contradicts the map two lines
        # down. It also dedupes MET/MSE altloc pairs, which a bare whitelist
        # would emit twice, and refuses free ligands.
        for residue in polymer.polymer_residues(chain_map[chain_id]):
            aa = _THREE_TO_ONE.get(residue.resname.strip())
            if aa is None:
                continue
            residue_numbers.append(residue.id[1])
            one_letter.append(aa)

        return residue_numbers, "".join(one_letter)
    except Exception:
        logger.debug("Chain sequence extraction failed.", exc_info=True)
        return [], ""


def _sequence_identity(seq_a: str, seq_b: str) -> float:
    """Compute approximate sequence identity between two protein sequences.

    Uses difflib.SequenceMatcher to count matching characters in the best
    common subsequence, divided by the length of the SHORTER sequence -- see
    the comment on `denom` below for why min and not max. This is suitable for
    the _MIN_VALIDATION_IDENTITY threshold check (0.70) but is not a rigorous
    pairwise alignment — use BLAST or biopython.Align for publication results.

    Matching BLOCKS are counted, not aligned positions, so a deletion can be
    FREE: a string that does not occur in the reference at all can still score
    1.0000. Read a perfect score as "no evidence of mismatch", not as
    "identical" -- that is how the hetflag gate that used to sit in
    _extract_chain_sequence stayed invisible for so long.

    A deletion can equally cost MORE than the residue removed, when it splits a
    repeated motif and the greedy block match cannot recover: DECDEDE ->
    DEDEDE scores 0.6667, not 6/6. Do not infer the size of a deletion's effect
    from the size of the deletion.

    Args:
        seq_a: First amino acid sequence (one-letter code).
        seq_b: Second amino acid sequence (one-letter code).

    Returns:
        Identity fraction in [0.0, 1.0]. Returns 0.0 if either sequence
        is empty.
    """
    if not seq_a or not seq_b:
        return 0.0
    matcher = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=False)
    matches = sum(block.size for block in matcher.get_matching_blocks())
    # Use min length as denominator: PDB chains are often domain constructs
    # covering only part of the full-length UniProt sequence (e.g. EGFR
    # extracellular domain = 621 aa vs full-length = 1210 aa). Using max
    # would reject all domain constructs at the 70% threshold.
    denom = min(len(seq_a), len(seq_b))
    return matches / denom if denom else 0.0
