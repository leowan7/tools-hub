"""PDB and mmCIF structure parser for Epitope Scout.

Provides a single entry point, parse_pdb(), that accepts a PDB or mmCIF file
path and returns a ParseResult containing per-chain residue counts, an error
string (empty on success), and a list of human-readable warning strings.

Key design decisions:
- HETATM records are excluded: only residues whose insertion-code tuple flag
  r.get_id()[0] == ' ' and whose resname is in STANDARD_AA are counted.
- NMR multi-model structures: only model index 0 is used to avoid summing
  residue counts across all conformational states.
- mmCIF input: detected by .cif extension; uses MMCIFParser(QUIET=True).
- PDB input: uses PDBParser(PERMISSIVE=True, QUIET=True) — logs warnings
  internally but does not raise on minor format deviations.
- All error strings are human-readable; raw exception text is never surfaced
  to callers.

Exports:
    STANDARD_AA  -- frozenset of 22 standard amino acid three-letter codes
    ChainInfo    -- dataclass: id (str), residue_count (int)
    ParseResult  -- dataclass: chains (list[ChainInfo]), error (str), warnings (list[str])
    parse_pdb    -- primary entry point
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from Bio.PDB import MMCIFParser, PDBParser

# ---------------------------------------------------------------------------
# Module-level logger — warnings go to the application log, not to callers.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard amino acid set.
# Includes the 20 canonical residues plus selenomethionine (MSE) and
# selenocysteine (SEC), which appear in deposited PDB structures.
# ---------------------------------------------------------------------------
STANDARD_AA: frozenset[str] = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS",
    "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO",
    "SER", "THR", "TRP", "TYR", "VAL",
    "MSE", "SEC",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChainInfo:
    """Information about a single protein chain extracted from a structure file.

    Attributes:
        id: Single-character chain identifier (e.g. 'A', 'B').
        residue_count: Number of standard amino acid residues in this chain.
            HETATM records (water, ligands, ions) are excluded.
        name: Molecule name from the file header (e.g. "Epidermal Growth Factor
            Receptor"). Empty string if not available in the header.
    """

    id: str
    residue_count: int
    name: str = ""


@dataclass
class ParseResult:
    """Result returned by parse_pdb().

    Attributes:
        chains: List of ChainInfo objects, one per protein chain found.
            Empty if parsing failed or no protein chains were detected.
        error: Human-readable error message. Empty string on success.
        warnings: List of human-readable warning strings. Non-empty when
            the structure has minor issues (e.g. missing residues in the
            electron density, multiple NMR models present).
    """

    chains: list = field(default_factory=list)
    error: str = ""
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Chain name extraction helpers
# ---------------------------------------------------------------------------

def _chain_names_from_pdb(structure) -> dict:
    """Build a chain_id → molecule name mapping from a PDB COMPND header.

    BioPython parses COMPND records into structure.header['compound'] — a dict
    keyed by mol_id ('1', '2', ...) where each value contains 'molecule' and
    'chain' fields. Chain IDs may be stored as a comma-separated string or list
    depending on the BioPython version.

    Args:
        structure: BioPython Structure object.

    Returns:
        Dict mapping uppercase chain ID to title-cased molecule name.
        Empty dict if no COMPND records are present.
    """
    chain_names: dict = {}
    compound = structure.header.get("compound", {})
    for mol in compound.values():
        molecule_name = str(mol.get("molecule", "")).strip().title()
        if not molecule_name:
            continue
        chains_val = mol.get("chain", "")
        if isinstance(chains_val, list):
            chain_ids = [c.strip().upper() for c in chains_val]
        else:
            chain_ids = [c.strip().upper() for c in str(chains_val).split(",")]
        for cid in chain_ids:
            if cid:
                chain_names[cid] = molecule_name
    return chain_names


def _chain_names_from_cif(path: Path) -> dict:
    """Build a chain_id → entity description mapping from an mmCIF file.

    Uses Bio.PDB.MMCIF2Dict to read _struct_asym (chain ↔ entity mapping) and
    _entity.pdbx_description (entity names) directly from the mmCIF data
    dictionary, bypassing the structure object which doesn't expose this info.

    Args:
        path: Path to the .cif file.

    Returns:
        Dict mapping uppercase chain ID to entity description string.
        Empty dict on parse failure.
    """
    try:
        from Bio.PDB.MMCIF2Dict import MMCIF2Dict  # noqa: PLC0415
        mmcif = MMCIF2Dict(str(path))

        # Build entity_id → description map.
        entity_ids = mmcif.get("_entity.id", [])
        entity_descs = mmcif.get("_entity.pdbx_description", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
            entity_descs = [entity_descs]
        entity_map = {
            eid: (str(edesc).strip().title() if edesc and str(edesc) not in (".", "?") else "")
            for eid, edesc in zip(entity_ids, entity_descs)
        }

        # Map chain (asym) IDs to entity descriptions.
        asym_ids = mmcif.get("_struct_asym.id", [])
        asym_entity_ids = mmcif.get("_struct_asym.entity_id", [])
        if isinstance(asym_ids, str):
            asym_ids = [asym_ids]
            asym_entity_ids = [asym_entity_ids]

        chain_names: dict = {}
        for cid, eid in zip(asym_ids, asym_entity_ids):
            name = entity_map.get(eid, "")
            if name:
                chain_names[cid.upper()] = name
        return chain_names
    except Exception:
        logger.debug("mmCIF chain name extraction failed.", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Primary entry point
# ---------------------------------------------------------------------------

def parse_pdb(pdb_path: Union[str, Path]) -> ParseResult:
    """Parse a PDB or mmCIF structure file and return chain information.

    Selects the first model (index 0) from the structure, which ensures
    correct behaviour for both single-model crystal structures and
    multi-model NMR ensembles. Only standard amino acid residues are counted
    per chain; water molecules and small-molecule ligands (HETATM records)
    are excluded.

    Args:
        pdb_path: Path (or str) to a .pdb or .cif structure file.

    Returns:
        ParseResult: On success, contains a non-empty chains list and an
            empty error string. On failure, contains an empty chains list
            and a human-readable error string. Warnings list is populated
            when non-fatal issues are detected (NMR ensemble, missing
            residues flagged in the file header).

    Raises:
        Nothing — all exceptions are caught and returned as ParseResult.error.
    """
    structure_path = Path(pdb_path)

    # ------------------------------------------------------------------
    # 1. Select parser based on file extension.
    # ------------------------------------------------------------------
    extension = structure_path.suffix.lower()
    if extension == ".cif":
        # MMCIFParser for .cif files; QUIET suppresses internal stderr noise.
        biopython_parser = MMCIFParser(QUIET=True)
        structure_id = structure_path.stem
    else:
        # PDBParser with PERMISSIVE=True: tolerates minor PDB format deviations
        # (e.g. non-standard line lengths, unusual ATOM records) without raising.
        biopython_parser = PDBParser(PERMISSIVE=True, QUIET=True)
        structure_id = structure_path.stem

    # ------------------------------------------------------------------
    # 2. Attempt to parse the file; catch all exceptions so callers always
    #    receive a ParseResult, never a raw exception.
    # ------------------------------------------------------------------
    try:
        structure = biopython_parser.get_structure(structure_id, str(structure_path))
    except Exception as parse_exception:
        logger.warning("Failed to parse %s: %s", structure_path, parse_exception)
        return ParseResult(
            error="Could not parse file. Verify this is a valid PDB or mmCIF file."
        )

    # ------------------------------------------------------------------
    # 3. Check whether Biopython produced any models at all.
    #    An empty structure indicates the file was not recognisable as PDB/mmCIF.
    # ------------------------------------------------------------------
    all_models = list(structure.get_models())
    if not all_models:
        return ParseResult(
            error="Could not parse file. Verify this is a valid PDB or mmCIF file."
        )

    warnings: list[str] = []

    # ------------------------------------------------------------------
    # 4. NMR multi-model notice.
    #    Surface this as a warning (not an error) so callers know model
    #    selection occurred. Model index 0 corresponds to PDB MODEL 1.
    # ------------------------------------------------------------------
    if len(all_models) > 1:
        warnings.append(
            f"NMR ensemble detected ({len(all_models)} models). "
            "Only model 0 (first conformer) is used for chain analysis."
        )

    selected_model = all_models[0]

    # Build chain_id → name lookup from file headers.
    if extension == ".cif":
        chain_name_map = _chain_names_from_cif(structure_path)
    else:
        chain_name_map = _chain_names_from_pdb(structure)

    # ------------------------------------------------------------------
    # 5. REMARK 465 — missing residues in electron density.
    #    Biopython parses this into structure.header['missing_residues']
    #    as a list of dicts. A non-empty list means some residues were
    #    not resolved and may create gaps in downstream SASA calculations.
    # ------------------------------------------------------------------
    missing_residue_entries = structure.header.get("missing_residues", [])
    if missing_residue_entries:
        count = len(missing_residue_entries)
        warnings.append(
            f"{count} unresolved (missing) residue(s) reported in the structure header "
            "(REMARK 465). SASA calculations may be affected at gap sites."
        )

    # ------------------------------------------------------------------
    # 6. Extract protein chains.
    #    A residue is counted as a protein residue when:
    #      - r.get_id()[0] == ' '  (flag ' ' = standard ATOM record; '
    #                                W' = water; 'H_XXX' = HETATM ligand)
    #      - r.resname.strip() is in STANDARD_AA
    # ------------------------------------------------------------------
    chain_infos: list[ChainInfo] = []

    for chain in selected_model.get_chains():
        protein_residues = [
            residue
            for residue in chain.get_residues()
            if residue.get_id()[0] == " " and residue.resname.strip() in STANDARD_AA
        ]
        if protein_residues:
            cid = chain.get_id()
            chain_infos.append(
                ChainInfo(
                    id=cid,
                    residue_count=len(protein_residues),
                    name=chain_name_map.get(cid.upper(), ""),
                )
            )

    # ------------------------------------------------------------------
    # 7. Guard: no protein chains found after filtering.
    # ------------------------------------------------------------------
    if not chain_infos:
        return ParseResult(
            error=(
                "No protein chains found. This file may contain only ligands, "
                "nucleic acids, or water molecules."
            )
        )

    return ParseResult(chains=chain_infos, warnings=warnings)
