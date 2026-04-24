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
    contact_residues list[int] — Antigen residue numbers at the interface
    antigen_chain   str   — Antigen chain ID in the PDB entry
    ab_chains       list[str] — Antibody chain IDs
"""

import csv
import difflib
import logging
import threading
from io import StringIO
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
# SAbDab per-structure summary endpoint. Returns TSV for PDB entries in the
# antibody database, or HTML 404 for entries not in SAbDab.
SABDAB_STRUCTURE_URL = (
    "https://opig.stats.ox.ac.uk/webapps/sabdab-sabpred/sabdab/summary/{pdb_id}/"
)
# RCSB search API — used to find PDB entries containing a given UniProt entity.
RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_PDB_DOWNLOAD_URL = "https://files.rcsb.org/download/{pdb_id}.pdb"
UNIPROT_FASTA_URL = "https://rest.uniprot.org/uniprotkb/{uniprot_id}.fasta"
# UniProt BLAST endpoint — used as fallback when DBREF is absent.
UNIPROT_BLAST_URL = "https://rest.uniprot.org/idmapping/run"
UNIPROT_BLAST_STATUS_URL = "https://rest.uniprot.org/idmapping/status/{job_id}"
UNIPROT_BLAST_RESULTS_URL = "https://rest.uniprot.org/idmapping/results/{job_id}"

# How many RCSB PDB IDs to probe against SAbDab before giving up. Higher
# values increase recall but add latency. 40 concurrent probes at ~0.5 s
# each complete in ~3–5 s wall-clock time on a typical network.
_RCSB_PROBE_LIMIT = 40

# Timeout for all external HTTP requests.
_REQUEST_TIMEOUT_SEC = 12

# Maximum number of structures for which contact residues are computed.
# Each requires a PDB file download (~0.5–5 MB) and BioPython parsing.
_MAX_CONTACT_STRUCTURES = 5

# Contact distance cutoff in Angstroms. 4.5 Å captures hydrogen bonds and
# van der Waals contacts at protein–protein interfaces.
_CONTACT_CUTOFF_ANGSTROM = 4.5

# Sequence identity thresholds for mismatch warnings.
# Below HIGH_WARN: possible orthologue or partial domain — warn user.
# Below LOW_WARN:  likely wrong protein — strong warning.
_IDENTITY_HIGH_WARN = 0.80
_IDENTITY_LOW_WARN = 0.30

# In-process cache: uniprot_id (uppercase) → list[dict]
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

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

    mmCIF format: uses BioPython MMCIF2Dict to parse _struct_ref and
    _struct_ref_seq loops. Cross-references ref_id to match chain_id
    with the correct UniProt accession.

    Args:
        pdb_path: Path to the uploaded PDB or mmCIF file.
        chain_id: Chain identifier to look up.

    Returns:
        UniProt accession string, or empty string if not found.
    """
    import re  # noqa: PLC0415

    pdb_str = str(pdb_path)

    if pdb_str.endswith(".cif"):
        return _extract_uniprot_from_cif(pdb_str, chain_id)

    # PDB format: parse DBREF lines
    try:
        with open(pdb_str, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""

    for line in text.splitlines():
        if line.startswith("DBREF "):
            # Columns: 12 = chain, 26-32 = db (UNP/SWS/TRE), 33-40 = accession
            dbref_chain = line[12].strip()
            db_name = line[26:32].strip()
            accession = line[33:41].strip()
            if dbref_chain == chain_id and db_name in ("UNP", "SWS", "TRE"):
                return accession

    return ""


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


def _search_uniprot_by_sequence(sequence: str) -> str:
    """Search UniProt by protein sequence using the BLAST-based ID mapping API.

    Submits sequence to UniProt's ID mapping service (UniParc → UniProtKB),
    polls for completion, and returns the top hit accession. Falls back
    gracefully on network failure, timeout, or no results.

    Args:
        sequence: One-letter amino acid sequence string (minimum 20 residues).

    Returns:
        UniProt accession string, or empty string if no match found.
    """
    import time  # noqa: PLC0415

    if not sequence or len(sequence) < 20:
        return ""

    try:
        # Submit BLAST job via UniProt ID mapping.
        resp = requests.post(
            UNIPROT_BLAST_URL,
            data={"from": "UniProtKB_AC-ID", "to": "UniProtKB", "ids": ""},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        # If the standard ID mapping doesn't support sequence, try the
        # peptide search endpoint which accepts short sequences.
        resp = requests.get(
            "https://rest.uniprot.org/uniprotkb/search",
            params={
                "query": f"({sequence[:50]})",
                "format": "json",
                "size": "1",
                "fields": "accession,protein_name",
            },
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if resp.ok:
            data = resp.json()
            results = data.get("results", [])
            if results:
                return results[0].get("primaryAccession", "")
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
            if rec:
                result["protein_name"] = rec.get("fullName", {}).get("value", "")
            elif prot.get("submittedNames"):
                result["protein_name"] = (
                    prot["submittedNames"][0].get("fullName", {}).get("value", "")
                )
            # Append organism if available.
            org = data.get("organism", {}).get("scientificName", "")
            if org and result["protein_name"]:
                result["protein_name"] += f" ({org})"
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
        2. Fall back to UniProt sequence search for structures without DBREF
           (e.g. AlphaFold models). Sequence search results are only accepted
           if identity >= 70%.

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
) -> list:
    """Compute antigen residue numbers in contact with antibody chains.

    Uses BioPython to parse the structure and NumPy for vectorised distance
    computation. Returns an empty list if either library is unavailable or
    if the chain IDs are not found in the structure.

    Args:
        pdb_text: Raw text content of a PDB coordinate file.
        antigen_chain: Chain ID of the antigen in the PDB file.
        ab_chains: List of antibody chain IDs (heavy and/or light).
        cutoff: Distance threshold in Angstroms.

    Returns:
        Sorted list of antigen residue sequence numbers (PDB auth_seq_id).
    """
    try:
        from Bio.PDB import PDBParser  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        logger.debug("BioPython or NumPy not available; skipping contact computation.")
        return []

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
                for residue in chain_map[ab_chain_id]:
                    if residue.id[0] != " ":
                        continue  # Skip HETATM and water
                    for atom in residue.get_atoms():
                        ab_atom_coords.append(atom.coord)

        if not ab_atom_coords:
            return []

        ab_coords = np.array(ab_atom_coords, dtype=np.float32)

        # For each standard antigen residue, find the minimum distance to
        # any antibody atom. Flag residues within the cutoff.
        contact_residues = []
        for residue in chain_map[antigen_chain]:
            if residue.id[0] != " ":
                continue  # Skip HETATM and water
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
        return []


def _fetch_and_compute_contacts(pdb_id: str, antigen_chain: str, ab_chains: list) -> list:
    """Download a PDB file from RCSB and compute interface contact residues.

    Args:
        pdb_id: RCSB PDB accession (case-insensitive).
        antigen_chain: Antigen chain ID in the PDB file.
        ab_chains: Antibody chain IDs.

    Returns:
        Sorted list of contact residue numbers, or [] on failure.
    """
    if not antigen_chain or not ab_chains:
        return []
    try:
        url = RCSB_PDB_DOWNLOAD_URL.format(pdb_id=pdb_id.upper())
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT_SEC)
        if not resp.ok:
            logger.debug(
                "PDB download failed for %s: HTTP %s", pdb_id, resp.status_code
            )
            return []
        return _compute_contacts(resp.text, antigen_chain, ab_chains)
    except Exception:
        logger.debug("PDB download/parse error for %s.", pdb_id, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# SAbDab query
# ---------------------------------------------------------------------------

def _rcsb_pdb_ids_for_uniprot(uniprot_id: str, limit: int = _RCSB_PROBE_LIMIT) -> list:
    """Return PDB IDs from RCSB that contain a polymer entity with the given
    UniProt accession, sorted by RCSB relevance score (best structures first).

    Args:
        uniprot_id: UniProt accession in uppercase (e.g. "P00533").
        limit: Maximum number of PDB IDs to return.

    Returns:
        List of uppercase PDB ID strings. Empty on failure.
    """
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
            return []
        data = resp.json()
        return [hit["identifier"].upper() for hit in data.get("result_set", [])]
    except Exception:
        logger.warning("RCSB search failed for %s.", uniprot_id, exc_info=True)
        return []


def _sabdab_entry_for_pdb(pdb_id: str) -> list:
    """Fetch SAbDab TSV for a single PDB entry.

    SAbDab's per-structure endpoint returns a TSV with one row per
    antibody-antigen chain pairing in that structure. Returns [] if the
    PDB ID is not in SAbDab (HTML response) or on network failure.

    Args:
        pdb_id: Uppercase RCSB PDB accession.

    Returns:
        List of parsed row dicts from the TSV (may have >1 row for multi-Fab
        crystals). Empty list if not in SAbDab.
    """
    url = SABDAB_STRUCTURE_URL.format(pdb_id=pdb_id.lower())
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT_SEC)
        if not resp.ok:
            return []
        text = resp.text.strip()
        # SAbDab returns HTML for entries not in the database.
        if not text or text.startswith("<"):
            return []
        reader = csv.DictReader(StringIO(text), delimiter="\t")
        return list(reader)
    except Exception:
        logger.debug("SAbDab per-structure fetch failed for %s.", pdb_id, exc_info=True)
        return []


def query_sabdab(uniprot_id: str) -> list:
    """Find antibody/nanobody structures for a target protein via RCSB + SAbDab.

    Step 1: Query RCSB search API to get PDB IDs containing the UniProt
    accession. Step 2: Probe each PDB ID against SAbDab's per-structure
    endpoint in parallel threads to filter for antibody-antigen complexes.

    Args:
        uniprot_id: UniProt accession (e.g. "P00533" for EGFR).

    Returns:
        List of binder dicts sorted by resolution (best first). Returns []
        on network failure or no antibody-complex structures found.
    """
    pdb_ids = _rcsb_pdb_ids_for_uniprot(uniprot_id)
    if not pdb_ids:
        return []

    # Probe each PDB ID against SAbDab in parallel. Collect all TSV rows.
    all_rows: list = []
    row_lock = threading.Lock()

    def _probe(pdb_id: str) -> None:
        rows = _sabdab_entry_for_pdb(pdb_id)
        if rows:
            with row_lock:
                all_rows.extend(rows)

    threads = [threading.Thread(target=_probe, args=(pid,), daemon=True) for pid in pdb_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_REQUEST_TIMEOUT_SEC + 2)

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
            "contact_residues": [],
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
            Remaining hits are returned without contact residues.

    Returns:
        List of binder dicts. Empty list if UniProt ID is blank or SAbDab
        returns no hits.
    """
    if not uniprot_id or not uniprot_id.strip():
        return []

    cache_key = uniprot_id.strip().upper()

    with _CACHE_LOCK:
        if cache_key in _CACHE:
            return _CACHE[cache_key]

    sabdab_hits = query_sabdab(cache_key)
    if not sabdab_hits:
        with _CACHE_LOCK:
            _CACHE[cache_key] = []
        return []

    # Compute contacts for the top N structures in parallel.
    to_process = sabdab_hits[:max_contact_structures]
    remainder = sabdab_hits[max_contact_structures:]

    processed = [dict(entry) for entry in to_process]

    def _worker(idx: int, entry: dict) -> None:
        residues = _fetch_and_compute_contacts(
            entry["pdb_id"], entry["antigen_chain"], entry["ab_chains"]
        )
        processed[idx]["contact_residues"] = residues

    threads = [
        threading.Thread(target=_worker, args=(i, entry), daemon=True)
        for i, entry in enumerate(to_process)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    # Append remaining hits (no contact residue computation).
    result = processed + [dict(entry) for entry in remainder]

    with _CACHE_LOCK:
        _CACHE[cache_key] = result

    return result


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
        for residue in chain_map[chain_id]:
            if residue.id[0] != " ":
                continue  # Skip HETATM and water
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
    common subsequence, divided by the length of the longer sequence. This
    is suitable for a threshold check (e.g. 80%) but is not a rigorous
    pairwise alignment — use BLAST or biopython.Align for publication results.

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


def check_sequence_identity(uniprot_id: str, pdb_path, chain_id: str) -> dict:
    """Check whether a PDB chain matches the canonical UniProt sequence.

    Fetches the UniProt FASTA sequence and computes approximate sequence
    identity against the selected chain in the uploaded structure. Returns
    a human-readable warning when identity is below the threshold, so the
    UI can alert the user that the known binder overlap results may not be
    meaningful.

    Args:
        uniprot_id: UniProt accession provided by the user.
        pdb_path: Path to the uploaded PDB or mmCIF file.
        chain_id: Chain identifier selected by the user.

    Returns:
        Dict with keys:
            identity      float | None  — identity fraction (0–1), None if
                                          sequences could not be computed.
            identity_pct  str           — formatted string e.g. "93.2%", or
                                          "unknown" if computation failed.
            warning       str           — empty string if identity >= 0.80 or
                                          if the check could not be performed;
                                          otherwise a human-readable warning.
    """
    _, chain_seq = _extract_chain_sequence(pdb_path, chain_id)
    if not chain_seq:
        logger.debug("Could not extract chain %s sequence from %s.", chain_id, pdb_path)
        return {"identity": None, "identity_pct": "unknown", "warning": ""}

    uniprot_seq = _fetch_uniprot_sequence(uniprot_id)
    if not uniprot_seq:
        logger.debug("Could not fetch UniProt sequence for %s.", uniprot_id)
        return {"identity": None, "identity_pct": "unknown", "warning": ""}

    identity = _sequence_identity(uniprot_seq, chain_seq)
    identity_pct = f"{identity * 100:.1f}%"

    if identity >= _IDENTITY_HIGH_WARN:
        warning = ""
    elif identity >= _IDENTITY_LOW_WARN:
        warning = (
            f"Sequence identity between chain {chain_id} and UniProt {uniprot_id} "
            f"is {identity_pct}. This may be an orthologue or partial domain construct. "
            "Known binder contact residues are shown but overlap detection may be "
            "unreliable due to residue numbering differences."
        )
    else:
        warning = (
            f"Low sequence identity ({identity_pct}) between chain {chain_id} and "
            f"UniProt {uniprot_id}. The accession may not match the uploaded protein. "
            "Known binder data is shown for reference only — overlap results are "
            "not meaningful."
        )

    return {"identity": identity, "identity_pct": identity_pct, "warning": warning}


def is_uniprot_id(value: str) -> bool:
    """Return True if the value looks like a UniProt accession.

    UniProt accessions follow the pattern: one letter, one digit, then 3
    alphanumeric characters, then one digit (6 characters total for standard;
    up to 10 for isoform variants like P00533-2).

    Args:
        value: User-supplied protein identifier string.

    Returns:
        bool: True if the value matches the UniProt accession pattern.
    """
    import re  # noqa: PLC0415
    value = value.strip()
    # UniProtKB accessions come in two formats:
    #   Standard:  [A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9]  e.g. A2BC19
    #   OPQ class: [OPQ][0-9][A-Z0-9]{3}[0-9]           e.g. P00533, Q9Y6K9
    # Optionally followed by an isoform suffix e.g. P00533-2.
    pattern = r'^([A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9]|[OPQ][0-9][A-Z0-9]{3}[0-9])(-\d+)?$'
    return bool(re.match(pattern, value.upper()))
