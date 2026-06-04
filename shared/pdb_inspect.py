"""Lightweight pre-flight PDB inspection for tools-hub uploads.

Runs at upload time, before ``create_job`` debits credits or hands the
file to Modal. Goals:

1. **Reject obvious garbage early** — no ATOM records, zero protein
   chains, parse failures. Show the user a clear error in the form,
   spend no credits, never create a tool_jobs row.
2. **Validate user-supplied hotspots** — if the user typed
   ``hotspots=35,52,62`` but their chain A only has residues 1..30,
   that's a fast pre-check, not a 30-second Modal round trip.
3. **Surface a structural summary** for logging / future UI panels —
   chain list with residue counts, HETATM resnames, multi-model flag.

This module is intentionally simpler than the Kendrew GPU-side
``pipeline_normalize.py``:

- It reads, it does not write a cleaned PDB. The Kendrew docker
  pipelines do the heavy normalization on the server side.
- It does not modify the input bytes. Whatever the user uploaded is
  what gets stored in Supabase Storage and handed to Modal.
- It is fast (Biopython parse of a typical 100KB PDB is <50ms).

The companion server-side module is
``llm-proteinDesigner/backend/pdb_utils/pipeline_normalize.py``.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Optional

from Bio.PDB import PDBParser, MMCIFParser, PDBIO

logger = logging.getLogger(__name__)


WATER_RESNAMES = frozenset(["HOH", "WAT", "H2O", "DOD", "TIP", "TIP3", "TIP4"])

STANDARD_AA = frozenset([
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
])

# Common aliases the GPU pipeline will remap (MSE->MET etc). We count these
# as "protein-equivalent" for the purposes of zero-protein detection so we
# don't spurious-reject a structure that's all-MSE.
MODRES_EQUIV = frozenset(["MSE", "CME", "CSO", "SEP", "TPO", "PTR", "KCX",
                          "HYP", "LLP"])


@dataclass
class ChainSummary:
    """Inspection summary for one chain."""
    chain_id: str
    standard_residue_count: int       # AA + MODRES_EQUIV
    hetatm_resnames: list = field(default_factory=list)  # 3-letter codes
    water_count: int = 0
    min_resnum: Optional[int] = None
    max_resnum: Optional[int] = None


@dataclass
class InspectionReport:
    """Full inspection result.

    ``ok`` is True iff the structure is acceptable to ship to GPU. When
    ``ok`` is False, ``error`` carries a user-facing message.

    ``altloc_atom_count`` is the number of ATOM/HETATM records whose
    altloc column is a non-blank letter (A, B, C, ...). The server-side
    normalizer collapses these to one record per atom name, but the count
    is useful for log triage and for surfacing a "we cleaned X alternate
    conformations off your upload" hint in the future.
    """
    ok: bool
    error: Optional[str] = None
    chains: list = field(default_factory=list)         # list[ChainSummary]
    model_count: int = 1
    total_standard_residues: int = 0
    total_hetatm_residues: int = 0
    total_water_residues: int = 0
    warnings: list = field(default_factory=list)       # list[str]
    altloc_atom_count: int = 0

    def chain_ids(self) -> list:
        return [c.chain_id for c in self.chains]

    def chain(self, cid: str) -> Optional[ChainSummary]:
        for c in self.chains:
            if c.chain_id == cid:
                return c
        return None


# -- Public API --------------------------------------------------------------

# Maximum upload size we'll inspect inline. Beyond this, we skip inspection
# and let the GPU pipeline reject it. (50 MB covers all realistic protein
# PDBs by orders of magnitude — most are <1 MB.)
MAX_INSPECT_BYTES = 50 * 1024 * 1024


def inspect_pdb_bytes(
    data: bytes, filename: str = "input.pdb",
) -> InspectionReport:
    """Inspect raw PDB or mmCIF bytes.

    Returns an InspectionReport with ``ok=True`` on success, ``ok=False``
    with a user-facing ``error`` on rejection. Parse exceptions are
    converted to ``ok=False`` with a friendly message — never raised.

    Args:
        data: Raw file content.
        filename: Used only to detect format from the extension.
    """
    if not data:
        return InspectionReport(ok=False, error="The uploaded file is empty.")
    if len(data) > MAX_INSPECT_BYTES:
        return InspectionReport(
            ok=False,
            error=(
                f"File too large ({len(data) // 1024} KB). "
                f"Maximum supported size is "
                f"{MAX_INSPECT_BYTES // (1024 * 1024)} MB."
            ),
        )

    fname_lower = (filename or "").lower()
    is_cif = fname_lower.endswith(".cif") or fname_lower.endswith(".mmcif")

    try:
        if is_cif:
            parser = MMCIFParser(QUIET=True)
            handle = io.StringIO(data.decode("utf-8", errors="replace"))
        else:
            parser = PDBParser(QUIET=True)
            handle = io.StringIO(data.decode("utf-8", errors="replace"))

        # Biopython will warn loudly on malformed input but parse what it can.
        structure = parser.get_structure("upload", handle)
    except Exception as exc:
        logger.info("pdb_inspect parse failed: %s", exc, exc_info=True)
        return InspectionReport(
            ok=False,
            error=(
                f"Could not parse the uploaded file as "
                f"{'mmCIF' if is_cif else 'PDB'}. "
                f"Error: {type(exc).__name__}."
            ),
        )

    models = list(structure.get_models())
    model_count = len(models)
    if model_count == 0:
        return InspectionReport(
            ok=False,
            error="The uploaded file contains no structural models.",
        )

    # Inspect first model only (multi-model NMR collapses to model 1
    # in the GPU pipeline).
    target_model = models[0]
    chains: list = []
    total_standard = 0
    total_hetatm = 0
    total_water = 0

    # Count alt-conformation atom records (any altloc that is not blank).
    # Biopython parses multi-altloc atoms into DisorderedAtom containers;
    # walking get_unpacked_list() yields all altloc copies.
    altloc_atom_count = 0
    for chain in target_model:
        for residue in chain:
            for atom in residue.get_unpacked_list():
                alt = (atom.get_altloc() or "").strip()
                if alt:
                    altloc_atom_count += 1

    for chain in target_model:
        cid = chain.get_id()
        std = 0
        hets: list = []
        waters = 0
        min_resnum: Optional[int] = None
        max_resnum: Optional[int] = None

        for residue in chain:
            hetflag, resnum, _icode = residue.get_id()
            resname = residue.get_resname().strip()
            is_hetatm = bool(hetflag and hetflag.strip())

            if resname in WATER_RESNAMES:
                waters += 1
                continue

            if resname in STANDARD_AA or resname in MODRES_EQUIV:
                std += 1
                if min_resnum is None or resnum < min_resnum:
                    min_resnum = resnum
                if max_resnum is None or resnum > max_resnum:
                    max_resnum = resnum
                continue

            if is_hetatm:
                hets.append(resname)

        chains.append(ChainSummary(
            chain_id=cid,
            standard_residue_count=std,
            hetatm_resnames=sorted(set(hets)),
            water_count=waters,
            min_resnum=min_resnum,
            max_resnum=max_resnum,
        ))
        total_standard += std
        total_hetatm += len(hets)
        total_water += waters

    report = InspectionReport(
        ok=True,
        chains=chains,
        model_count=model_count,
        total_standard_residues=total_standard,
        total_hetatm_residues=total_hetatm,
        total_water_residues=total_water,
        altloc_atom_count=altloc_atom_count,
    )

    if model_count > 1:
        report.warnings.append(
            f"Multi-model NMR ensemble detected ({model_count} models); "
            f"only model 1 will be used."
        )
    if altloc_atom_count:
        report.warnings.append(
            f"Found {altloc_atom_count} alternate-conformation atom record(s) "
            f"(altloc A/B/C). The server will keep the highest-occupancy "
            f"conformation per atom before running the pipeline."
        )
    if total_standard == 0:
        report.ok = False
        report.error = (
            "The uploaded file contains no standard protein residues. "
            "Check that the file has ATOM records for amino acids "
            "(not just HETATM ligands or waters)."
        )
        return report
    if not chains:
        report.ok = False
        report.error = (
            "No chains found in the uploaded file's first model."
        )
        return report

    return report


class CifConversionError(Exception):
    """Raised when CIF -> PDB conversion fails. Message is user-facing."""


def convert_cif_to_pdb_bytes(data: bytes, filename: str = "input.cif") -> bytes:
    """Convert mmCIF bytes to PDB bytes via Biopython.

    ProteinMPNN's parser (and several Kendrew docker pipelines) is
    PDB-column-strict and cannot parse CIF text. This converter runs
    server-side after ``inspect_pdb_bytes`` has already validated that
    the structure has protein content, so the parse is expected to
    succeed in the common case.

    Raises ``CifConversionError`` with a user-facing message on parse or
    write failure. The caller surfaces the message via the form's
    error render path.
    """
    if not data:
        raise CifConversionError("Empty file passed to CIF converter.")
    try:
        parser = MMCIFParser(QUIET=True)
        handle = io.StringIO(data.decode("utf-8", errors="replace"))
        structure = parser.get_structure("upload", handle)
    except Exception as exc:
        logger.info("cif_convert parse failed: %s", exc, exc_info=True)
        raise CifConversionError(
            f"Could not parse {filename} as mmCIF for PDB conversion "
            f"({type(exc).__name__})."
        ) from exc
    out = io.StringIO()
    try:
        io_writer = PDBIO()
        io_writer.set_structure(structure)
        io_writer.save(out)
    except Exception as exc:
        logger.info("cif_convert write failed: %s", exc, exc_info=True)
        raise CifConversionError(
            f"Could not write {filename} as PDB. The structure may have "
            f"multi-character chain IDs or more than 99,999 atoms, which "
            f"the legacy PDB format cannot represent."
        ) from exc
    return out.getvalue().encode("utf-8")


def validate_target_chain(
    report: InspectionReport, target_chain: str,
) -> Optional[str]:
    """Confirm the user-typed target_chain exists with protein residues.

    Returns None on success, or a user-facing error string on failure.
    Case-sensitive (matches Biopython chain IDs). When a user types ``a``
    but the file has chain ``A``, we return an error rather than guessing.
    """
    if not target_chain:
        return "Target chain is required."
    chain = report.chain(target_chain)
    if chain is None:
        present = report.chain_ids()
        return (
            f"Target chain '{target_chain}' is not in the uploaded file. "
            f"Found chain(s): {', '.join(present) if present else '(none)'}."
        )
    if chain.standard_residue_count == 0:
        return (
            f"Chain '{target_chain}' has no standard protein residues. "
            f"It contains "
            f"{len(chain.hetatm_resnames)} ligand record(s)."
        )
    return None


def validate_hotspots(
    report: InspectionReport, target_chain: str, hotspots: list,
) -> tuple[list, list]:
    """Cross-check user-supplied hotspots against the target chain range.

    Returns (in_range, out_of_range). The caller decides whether
    out_of_range constitutes a hard reject (recommended) or a warning.

    Hotspots refer to original PDB author numbering, which is what
    Kendrew docker pipelines accept on the wire (the docker side
    rewrites them via the renumber_map after CIF prep).
    """
    chain = report.chain(target_chain)
    if chain is None or chain.min_resnum is None:
        return [], list(hotspots)
    in_range: list = []
    out_of_range: list = []
    for h in hotspots:
        try:
            n = int(h)
        except (TypeError, ValueError):
            out_of_range.append(h)
            continue
        if chain.min_resnum <= n <= chain.max_resnum:
            in_range.append(n)
        else:
            out_of_range.append(n)
    return in_range, out_of_range


def summarize_for_log(report: InspectionReport) -> str:
    """Compact one-line summary suitable for ``logger.info``."""
    parts: list = [
        f"models={report.model_count}",
        f"chains={len(report.chains)}",
        f"protein_res={report.total_standard_residues}",
        f"hetatm={report.total_hetatm_residues}",
        f"water={report.total_water_residues}",
        f"altloc={report.altloc_atom_count}",
    ]
    for chain in report.chains:
        parts.append(
            f"{chain.chain_id}=({chain.standard_residue_count} aa, "
            f"{chain.water_count} hoh, "
            f"{len(chain.hetatm_resnames)} het={chain.hetatm_resnames or '-'}, "
            f"resnum {chain.min_resnum}..{chain.max_resnum})"
        )
    return " ".join(parts)
