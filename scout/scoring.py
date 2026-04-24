"""Per-patch and per-residue scoring for epitope surface patches (STRUCT-03/04/05).

Three scoring dimensions are computed here and combined downstream to rank
candidate epitope regions for binder design:

  - Geometry score (STRUCT-03): Pure surface accessibility (inverted burial),
    normalized across all patches to [0, 1]. Chemistry is handled separately
    by hydrophobic_exposure and hot_spot_density in the composite score.
  - B-factor score (STRUCT-04): Z-scored backbone B-factor inverted so that
    rigid (low B-factor) residues score high. Detects AlphaFold pLDDT and
    maps it directly (high pLDDT = rigid = high score).
  - DSSP secondary structure (STRUCT-05): Maps DSSP codes to helix/strand/loop
    with a graceful fallback when mkdssp binary is absent.

Exports:
    HYDROPHOBIC_AA           -- frozenset of 8 nonpolar residue names
    BURIAL_RADIUS            -- float, heavy-atom count radius for burial proxy
    DSSP_HELIX_CODES         -- frozenset of DSSP codes mapped to "helix"
    DSSP_STRAND_CODES        -- frozenset of DSSP codes mapped to "strand"
    is_likely_plddt          -- detect AlphaFold pLDDT in B-factor column
    score_geometry           -- compute raw burial + hydrophobicity for one patch
    normalize_burial_scores  -- min-max normalize burial across all patches
    compute_bfactor_scores   -- Z-scored B-factor -> per-residue [0,1] score
    assign_dssp              -- DSSP secondary structure with fallback
"""

from __future__ import annotations

import logging

import numpy as np
from Bio.PDB.DSSP import DSSP

from scout.patches import get_cb_coord

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Standard nonpolar residue set used in epitope/binder design literature
# to define hydrophobic patch character.
HYDROPHOBIC_AA: frozenset = frozenset({
    "ALA", "VAL", "ILE", "LEU", "MET", "PHE", "TRP", "PRO"
})

# Angstrom radius for counting heavy atoms around the patch centroid.
# 8 Å is a standard sphere for burial proxy in epitope scoring
# (Thornton et al. 1986; ProShape / patch analysis literature).
BURIAL_RADIUS: float = 8.0

# DSSP secondary structure code mappings.
# Codes: H=alpha-helix, G=3-10-helix, I=pi-helix -> "helix"
#        E=beta-strand, B=beta-bridge -> "strand"
#        All others (T=turn, S=bend, C=coil, " "=loop) -> "loop"
DSSP_HELIX_CODES: frozenset = frozenset({"H", "G", "I"})
DSSP_STRAND_CODES: frozenset = frozenset({"E", "B"})


# ---------------------------------------------------------------------------
# Geometry scoring — STRUCT-03
# ---------------------------------------------------------------------------

def score_geometry(patch_residues: list, all_atom_coords: np.ndarray) -> dict:
    """Compute raw burial and hydrophobicity scores for a single patch.

    Burial is approximated as the count of heavy atoms within BURIAL_RADIUS
    angstroms of the patch Cb centroid. Hydrophobicity is the fraction of
    patch residues that are in HYDROPHOBIC_AA.

    The scores are "raw" in the sense that burial is an integer count.
    Call normalize_burial_scores() on a list of these dicts to add the
    final geometry_score in [0, 1].

    Args:
        patch_residues: List of Biopython Residue objects forming one patch.
        all_atom_coords: (N, 3) numpy array of all heavy atom coordinates
            in the chain. Used for burial counting.

    Returns:
        Dict with keys:
            "burial_raw"     -- int, heavy-atom count within BURIAL_RADIUS
            "hydrophobicity" -- float in [0, 1], fraction of hydrophobic residues
    """
    # -- Compute Cb centroid of the patch --------------------------------
    cb_coords = []
    for residue in patch_residues:
        coord = get_cb_coord(residue)
        if coord is not None:
            cb_coords.append(coord)

    if not cb_coords:
        # No valid Cb coordinates in this patch — return zero scores.
        return {"burial_raw": 0, "hydrophobicity": 0.0}

    centroid = np.mean(cb_coords, axis=0)  # shape (3,)

    # -- Count heavy atoms within BURIAL_RADIUS of centroid --------------
    # Vectorized distance from all chain atoms to the centroid.
    distances = np.linalg.norm(all_atom_coords - centroid, axis=1)
    burial_raw = int(np.sum(distances <= BURIAL_RADIUS))

    # -- Hydrophobicity fraction -----------------------------------------
    num_residues = len(patch_residues)
    if num_residues == 0:
        hydrophobicity = 0.0
    else:
        num_hydrophobic = sum(
            1 for res in patch_residues if res.resname in HYDROPHOBIC_AA
        )
        hydrophobicity = num_hydrophobic / num_residues

    return {"burial_raw": burial_raw, "hydrophobicity": hydrophobicity}


def normalize_burial_scores(patches_data: list[dict]) -> list[dict]:
    """Add geometry_score to each patch dict via min-max burial normalization.

    geometry_score = accessibility (pure shape, no chemistry)

    Burial is INVERTED so that flat, exposed surfaces score high and deeply
    buried/concave pockets score low. Chemistry (hydrophobicity, hot spots)
    is handled by separate composite terms to avoid double-counting.

    accessibility = 1.0 - (burial_raw - min) / (max - min)

    When all patches have the same burial_raw (max == min), accessibility
    is set to 1.0 for all patches.

    The input list is mutated in place (geometry_score key is added) and
    also returned for convenience.

    Args:
        patches_data: List of dicts, each containing at minimum "burial_raw"
            (int). Produced by score_geometry().

    Returns:
        The same list, with "geometry_score" (float, rounded to 3 dp) added
        to each dict.
    """
    if not patches_data:
        return patches_data

    burial_values = [d["burial_raw"] for d in patches_data]
    burial_min = min(burial_values)
    burial_max = max(burial_values)

    # Guard: when all patches have identical burial, set denominator to 1
    # to avoid division by zero. All patches get accessibility = 1.0.
    denom = burial_max - burial_min if burial_max != burial_min else 1.0

    for patch_dict in patches_data:
        # Invert: low burial (exposed, flat) -> high accessibility score
        # Pure shape metric — no hydrophobicity component.
        accessibility = 1.0 - (patch_dict["burial_raw"] - burial_min) / denom
        patch_dict["geometry_score"] = round(accessibility, 3)

    return patches_data


# ---------------------------------------------------------------------------
# B-factor scoring — STRUCT-04
# ---------------------------------------------------------------------------

def _is_experimental_structure(pdb_path: str) -> bool:
    """Check PDB/mmCIF header for experimental method records.

    Returns True if the file contains EXPDTA (PDB) or _exptl.method
    (mmCIF) indicating an experimental structure (X-ray, NMR, cryo-EM).
    AlphaFold models lack these records or have 'THEORETICAL MODEL'.

    Args:
        pdb_path: Path to the structure file.

    Returns:
        True if experimental method detected, False otherwise.
    """
    experimental_methods = {
        "X-RAY DIFFRACTION", "SOLUTION NMR", "SOLID-STATE NMR",
        "ELECTRON MICROSCOPY", "ELECTRON CRYSTALLOGRAPHY",
        "NEUTRON DIFFRACTION", "FIBER DIFFRACTION",
    }
    try:
        with open(pdb_path, "r", errors="replace") as fh:
            for line in fh:
                # PDB format
                if line.startswith("EXPDTA"):
                    method = line[10:].strip().upper().rstrip(";").strip()
                    if any(m in method for m in experimental_methods):
                        return True
                    if "THEORETICAL MODEL" in method:
                        return False
                # mmCIF format
                if "_exptl.method" in line:
                    method = line.split()[-1].strip("'\"").upper()
                    if any(m in method for m in experimental_methods):
                        return True
                # Stop scanning after we hit coordinates
                if line.startswith("ATOM") or line.startswith("HETATM"):
                    break
                if line.startswith("_atom_site."):
                    break
    except OSError:
        pass
    return False


def is_likely_plddt(chain_residues: list, pdb_path: str = "") -> bool:
    """Detect if B-factor column likely contains AlphaFold pLDDT scores.

    Two-step detection:
    1. If the file header contains an experimental method (EXPDTA / _exptl.method),
       it is NOT pLDDT regardless of B-factor values.
    2. Otherwise, apply B-factor heuristic: all values in [0, 100] with mean > 50.

    Args:
        chain_residues: List of Biopython Residue objects from a single chain.
        pdb_path: Path to the structure file (for header check).

    Returns:
        True if the B-factor column likely contains pLDDT values.
    """
    # Step 1: header-based check (most reliable)
    if pdb_path and _is_experimental_structure(str(pdb_path)):
        return False

    # Step 2: B-factor heuristic fallback
    backbone_atoms = ("N", "CA", "C", "O")
    bfacs = []
    for residue in chain_residues:
        for atom_name in backbone_atoms:
            try:
                bfacs.append(residue[atom_name].get_bfactor())
            except KeyError:
                pass
    if len(bfacs) < 40:  # ~10 residues x 4 backbone atoms
        return False
    bfac_arr = np.array(bfacs)
    if float(np.min(bfac_arr)) < 0.0 or float(np.max(bfac_arr)) > 100.0:
        return False
    return float(np.mean(bfac_arr)) > 50.0


def compute_bfactor_scores(chain_residues: list, plddt_mode: bool = False) -> dict:
    """Compute per-residue B-factor scores as inverted Z-scores in [0, 1].

    Backbone atoms (N, CA, C, O) mean B-factor is computed per residue.
    Residues with no backbone atoms present are skipped.

    Two modes:

    **Crystallographic (default):** Z-score inverted so that low B-factor
    (rigid) residues score high.
        z = (bfac_mean - chain_mean) / chain_std
        score = 1.0 - clip(z, 0.0, 3.0) / 3.0

    **pLDDT mode (plddt_mode=True):** AlphaFold pLDDT values are mapped
    directly: high pLDDT (confident/rigid) → high score.
        score = clip(pLDDT, 0, 100) / 100

    Guard: if chain_std == 0 (all B-factors identical), chain_std is set
    to 1.0. All Z-scores become 0.0 and all scores return 1.0.

    Args:
        chain_residues: List of Biopython Residue objects from a single chain.
        plddt_mode: If True, treat B-factor column as AlphaFold pLDDT.

    Returns:
        Dict mapping residue.get_full_id() to float score in [0, 1].
        Rounded to 3 decimal places.
    """
    backbone_atoms = ("N", "CA", "C", "O")

    # -- Collect per-residue backbone mean B-factor ----------------------
    residue_bfactors: list[tuple] = []  # list of (full_id, mean_bfac)

    for residue in chain_residues:
        present_bfacs = []
        for atom_name in backbone_atoms:
            try:
                atom = residue[atom_name]
                present_bfacs.append(atom.get_bfactor())
            except KeyError:
                pass

        if not present_bfacs:
            continue

        mean_bfac = float(np.mean(present_bfacs))
        residue_bfactors.append((residue.get_full_id(), mean_bfac))

    if not residue_bfactors:
        return {}

    # -- pLDDT mode: direct mapping, no Z-scoring -----------------------
    if plddt_mode:
        scores = {}
        for full_id, mean_bfac in residue_bfactors:
            score = float(np.clip(mean_bfac, 0.0, 100.0)) / 100.0
            scores[full_id] = round(score, 3)
        return scores

    # -- Crystallographic mode: Z-score across the chain -----------------
    bfac_array = np.array([bfac for _, bfac in residue_bfactors])
    chain_mean = float(np.mean(bfac_array))
    chain_std = float(np.std(bfac_array))

    # Guard: zero std (all identical B-factors) -> treat as std = 1.0 so
    # all Z-scores are 0.0 and all scores return 1.0.
    if chain_std == 0.0:
        chain_std = 1.0

    scores = {}
    for full_id, mean_bfac in residue_bfactors:
        z_score = (mean_bfac - chain_mean) / chain_std
        # Clip to [0, 3]: residues below mean (low B-factor) clip to 0
        # giving score 1.0; residues far above mean clip at 3 giving 0.0.
        score = 1.0 - float(np.clip(z_score, 0.0, 3.0)) / 3.0
        scores[full_id] = round(score, 3)

    return scores


# ---------------------------------------------------------------------------
# DSSP secondary structure — STRUCT-05
# ---------------------------------------------------------------------------

def _assign_ss_by_phi_psi(model) -> dict:
    """Fallback SS assignment using backbone phi/psi angles (Ramachandran).

    Used when mkdssp is not installed. Classifies each residue based on
    its backbone dihedral angles into generous Ramachandran regions:

        Helix:  -160 < phi < -20  AND  -80 < psi < 10
        Strand: -180 < phi < -60  AND  (100 < psi < 180  OR  -180 < psi < -120)
        Loop:   everything else

    These boundaries are deliberately generous to avoid under-assigning
    regular secondary structure. The classification is less accurate than
    DSSP (no hydrogen bond analysis) but sufficient for scoring purposes.

    Args:
        model: Biopython Model object (structure[0]).

    Returns:
        Dict mapping (chain_id, residue.get_id()) to one of
        "helix", "strand", or "loop".
    """
    from Bio.PDB.Polypeptide import PPBuilder  # noqa: PLC0415

    ss_map = {}
    ppb = PPBuilder()

    for chain in model.get_chains():
        chain_id = chain.get_id()
        for pp in ppb.build_peptides(chain):
            phi_psi = pp.get_phi_psi_list()
            for residue, (phi, psi) in zip(pp, phi_psi):
                if phi is None or psi is None:
                    label = "loop"
                else:
                    phi_deg = np.degrees(phi)
                    psi_deg = np.degrees(psi)
                    if -160 < phi_deg < -20 and -80 < psi_deg < 10:
                        label = "helix"
                    elif (-180 < phi_deg < -60
                          and (100 < psi_deg < 180 or -180 < psi_deg < -120)):
                        label = "strand"
                    else:
                        label = "loop"
                key = (chain_id, residue.get_id())
                ss_map[key] = label

    return ss_map


def assign_dssp(model, pdb_path: str) -> dict:
    """Assign secondary structure labels to residues using DSSP.

    Calls Bio.PDB.DSSP with the mkdssp binary. If mkdssp is not installed
    or the PDB file cannot be read, falls back to phi/psi Ramachandran
    classification (pure Python, no external binary required).

    DSSP code mapping:
        H, G, I -> "helix"
        E, B    -> "strand"
        all else -> "loop" (T, S, C, ' ')

    Args:
        model: Biopython Model object (structure[0]).
        pdb_path: Path to the PDB file on disk (required by DSSP wrapper).

    Returns:
        Dict mapping DSSP keys to one of "helix", "strand", or "loop".
        Falls back to phi/psi classification if DSSP binary is unavailable.
    """
    try:
        dssp_obj = DSSP(model, pdb_path, dssp="mkdssp")
        ss_map = {}
        for dssp_key in dssp_obj.property_keys:
            residue_data = dssp_obj[dssp_key]
            ss_code = residue_data[1]
            if ss_code in DSSP_HELIX_CODES:
                label = "helix"
            elif ss_code in DSSP_STRAND_CODES:
                label = "strand"
            else:
                label = "loop"
            ss_map[dssp_key] = label
        return ss_map
    except Exception as exc:
        logger.warning(
            "DSSP binary unavailable (%s); falling back to phi/psi classification",
            exc,
        )
        try:
            return _assign_ss_by_phi_psi(model)
        except Exception as fallback_exc:
            logger.warning(
                "Phi/psi fallback also failed (%s); all residues default to 'loop'",
                fallback_exc,
            )
            return {}
