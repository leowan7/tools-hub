"""PDB and mmCIF structure parser for Epitope Scout.

Provides a single entry point, parse_pdb(), that accepts a PDB or mmCIF file
path and returns a ParseResult containing per-chain residue counts, an error
string (empty on success), and a list of human-readable warning strings.

Key design decisions:
- HETATM records are excluded, EXCEPT the modified amino acids in
  _MODIFIED_AA (MSE, SEC). Those are ordinary polymer residues that the PDB
  deposits as HETATM, so gating them on the hetflag drops them from the
  chain length. A FREE MSE/SEC ligand is therefore counted too -- it cannot
  be told from a chain link here. See _is_polymer_residue.
- NMR multi-model structures: only model index 0 is used to avoid summing
  residue counts across all conformational states.
- mmCIF input: detected by .cif extension; uses MMCIFParser(QUIET=True).
- PDB input: uses PDBParser(PERMISSIVE=True, QUIET=True) — logs warnings
  internally but does not raise on minor format deviations.
- All error strings are human-readable; raw exception text is never surfaced
  to callers.

Exports:
    STANDARD_AA  -- frozenset of 22 standard amino acid three-letter codes
    ChainInfo    -- dataclass: id (str), residue_count (int), name (str)
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
#
# The two modified names are split into their own frozenset only so the
# selector below can name them. STANDARD_AA is still exactly the same 22
# names it has always held. Its MSE/SEC members are now belt-and-braces:
# _is_polymer_residue short-circuits on _MODIFIED_AA before ever reaching
# them. Nothing outside this module imports STANDARD_AA -- scout/pipeline.py
# and scout/scoring.py both import the 20-name set from scout/sasa.py -- so
# it is documented surface rather than an imported one -- though
# test_standard_aa_is_still_the_documented_22_names now pins its members.
# ---------------------------------------------------------------------------
_MODIFIED_AA: frozenset[str] = frozenset({"MSE", "SEC"})

STANDARD_AA: frozenset[str] = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS",
    "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO",
    "SER", "THR", "TRP", "TYR", "VAL",
}) | _MODIFIED_AA


def _is_polymer_residue(residue) -> bool:
    """True when this residue counts toward a chain's length.

    Accepts the canonical 20 spelled as ATOM, plus MSE and SEC however they
    are spelled. The modified pair is matched on resname ALONE: the PDB
    deposits MSE as HETATM, so gating it on the hetflag left STANDARD_AA's own
    MSE/SEC entries reachable only for the ATOM spelling. Measured: 1B24 chain
    A, three HETATM MSE and no MET, read 170 of 173; 1CC1 chain L, whose SEC
    492 is deposited as ATOM, read 487 and still does. 1CC1 carries no MODRES
    record at all, so MODRES is not a usable signal for this decision.

    THE HETFLAG GATE STAYS ON THE CANONICAL 20. Same rule and same reason as
    scout/scoring.py::_is_pydssp_polymer_residue: with no peptide-bond
    continuity test here, the hetflag is the only thing separating a free
    solvent residue from a chain link. scoring.py::_ScoutPPBuilder can drop
    the gate outright because Biopython's _is_connected then rejects the free
    residue on geometry; nothing does that here.

    Kept separate from scoring.py's copy deliberately. They agree on every
    input today but serve different subsystems -- the analysis path filters
    against the 20-name set in scout/sasa.py, which scout/pipeline.py imports
    over this module's, so MSE never enters a patch at all. Sharing one predicate would
    let a change to either silently retune the other. Why the analysis path
    excludes MSE is argued in scoring.py's _MODIFIED_AA comment block and is
    not a claim this module has measured.

    THIS CHANGES WHICH CHAINS EXIST, not only their counts. A chain built
    wholly of modified residues previously had no residues at all, so
    parse_pdb's ``if protein_residues:`` dropped it and the picker never
    offered it.

    RESIDUAL RISK, the same one that comment block's "Residual risk"
    paragraph already accepts: a free MSE/SEC ligand is indistinguishable
    from a chain link here, so it adds one to the count -- unless its
    (resseq, icode) collides with a polymer position, which the dedupe in
    parse_pdb then absorbs. Two knock-on effects, both traced by reading
    scout/routes.py and NOT exercised (freesasa is absent from the dev env,
    so neither response code was observed). A ligand-only chain carrying one
    now reaches the picker as a dead end the analysis path should refuse with
    a 422. And a file whose ONLY amino acid is such a ligand now parses, so
    upload should answer 200 where it used to answer 422 -- which means the
    job directory survives instead of being removed by the rmtree on the
    parse-failure branch, and counts against that session's
    ANON_MAX_LIVE_JOBS_PER_SESSION budget until it is reaped.

    Prior art worth knowing before changing the whitelist:
    shared/pdb_inspect.py::MODRES_EQUIV solves the same problem for the
    campaign path with NINE names and no hetflag gate at all. Two names here
    is a deliberately narrower choice -- test_the_modified_residue_whitelist
    _does_not_admit_ligands_or_water pins SEP out -- not an oversight.
    """
    resname = residue.resname.strip()
    if resname in _MODIFIED_AA:
        return True
    return residue.get_id()[0] == " " and resname in STANDARD_AA


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChainInfo:
    """Information about a single protein chain extracted from a structure file.

    Attributes:
        id: Single-character chain identifier (e.g. 'A', 'B').
        residue_count: Number of amino acid residues in this chain, one per
            sequence position, including MSE/SEC however spelled. Water and
            other heteroatoms are excluded -- except a free MSE/SEC ligand,
            which is indistinguishable from a chain link here. See
            _is_polymer_residue. NOT display-only: scout/routes.py derives
            the epitope ranking's patch-size cap (_max_resi) and the
            "terminal patch" flag's chain_length from this number.
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
    multi-model NMR ensembles. Only amino acid residues are counted per
    chain, including the modified amino acids MSE and SEC however they are
    spelled (see _is_polymer_residue); water and other heteroatoms are
    excluded, though a free MSE/SEC ligand cannot be told from a polymer
    residue here. Each sequence position counts once, even when it carries
    two altlocs with different resnames.

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
    #    _is_polymer_residue decides what counts: the canonical 20 as ATOM
    #    records, plus MSE/SEC however they are spelled. Water ('W') stays
    #    out, and so does every HETATM ligand except a free MSE/SEC, which
    #    cannot be told from a chain link here.
    # ------------------------------------------------------------------
    chain_infos: list[ChainInfo] = []

    for chain in selected_model.get_chains():
        # One count per sequence position. Partial selenomethionine
        # incorporation deposits MET and MSE at the SAME residue number as two
        # altlocs; because their hetflags differ (" " vs "H_MSE") Biopython
        # hands them back as two separate residues rather than one
        # DisorderedResidue, and both now satisfy _is_polymer_residue. Without
        # this, admitting MSE would count that position twice -- making the
        # length WORSE than the bug this function exists to fix. Measured on a
        # 3-residue chain with one such pair: 4 before this guard, 3 after.
        #
        # The key also absorbs a free MSE/SEC ligand that happens to reuse a
        # polymer residue number. That is a different case, not a duplicate
        # spelling, and one count is the answer we want for both.
        seen_positions = set()
        protein_residues = []
        for residue in chain.get_residues():
            if not _is_polymer_residue(residue):
                continue
            position = residue.get_id()[1:]  # (resseq, icode)
            if position in seen_positions:
                continue
            seen_positions.add(position)
            protein_residues.append(residue)
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
