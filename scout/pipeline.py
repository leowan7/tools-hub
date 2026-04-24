"""Orchestrator pipeline for Epitope Scout structural scoring (STRUCT-01..05).

This module exposes a single entry point, run_pipeline(), which coordinates
the full analysis from PDB file to results.csv:

    1. Parse PDB (Biopython PDBParser)
    2. Validate chain exists
    3. Compute per-residue RSA (freesasa via sasa.py)
    4. Filter surface residues (RSA >= SURFACE_RSA_THRESHOLD, standard AA only)
    5. Cluster surface residues into patches (patches.py)
    6. Score each patch: geometry, B-factor, DSSP secondary structure (scoring.py)
    7. Write results.csv and return its Path

This is the single function called by the Flask /analyze route.

Exports:
    run_pipeline  -- primary entry point
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from pathlib import Path

import numpy as np
from Bio.PDB import MMCIFParser, PDBParser

from scout.patches import cluster_surface_residues, get_cb_coord
from scout.sasa import STANDARD_AA, SURFACE_RSA_THRESHOLD, compute_rsa
from scout.scoring import (
    assign_dssp,
    compute_bfactor_scores,
    is_likely_plddt,
    normalize_burial_scores,
    score_geometry,
)

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSV column order — must stay in sync with the CONTEXT.md specification
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "epitope_id",
    "residues",
    "residue_count",
    "mean_rsa",
    "composite_score",
    "hydrophobic_exposure",
    "hydrophobicity",
    "hot_spot_fraction",
    "geometry_score",
    "burial_raw",
    "mean_bfactor",
    "bfactor_score",
    "secondary_structure",
    "centroid_x",
    "centroid_y",
    "centroid_z",
    "is_plddt",
]

# ---------------------------------------------------------------------------
# Composite scoring weights for binder design candidacy
# ---------------------------------------------------------------------------
#
# Composite score = 0.30 * hydrophobic_exposure   (free energy proxy)
#                 + 0.20 * bfactor_score           (rigidity)
#                 + 0.15 * geometry_score           (surface accessibility)
#                 + 0.20 * ss_score                 (structural order)
#                 + 0.15 * hot_spot_fraction        (hot spot density)
#
# hydrophobic_exposure = hydrophobicity * mean_rsa
#   Captures available desolvation free energy: only accessible hydrophobic
#   surface contributes to ΔG upon binder contact. Buried hydrophobics
#   provide no benefit. (Chothia 1974; Vajda et al. 2018)
#
# geometry_score = 0.5 * accessibility + 0.5 * hydrophobicity
#   Accessibility is INVERTED burial: flat, exposed surfaces score high,
#   concave pockets score low. De novo binders need physical access to
#   the epitope — pocketed regions are sterically inaccessible.
#   (Lo Conte et al. 1999; Lawrence & Colman 1993)
#
# ss_score: INDEPENDENT secondary structure term. Natural PPI interfaces
#   are dominated by helices and strands (Lo Conte et al. 1999). Loops
#   are flexible and may rearrange in solution, leading to design failure.
#   Making SS a first-class scoring component (20% weight) ensures that
#   structured epitopes are prioritized over loops with high RSA.
#   strand = 1.0 (flat, rigid, best shape complementarity)
#   helix  = 0.8 (rigid, regular, good for helical binder scaffolds)
#   loop   = 0.2 (flexible, conformationally uncertain)
#
# hot_spot_fraction: Trp, Tyr, Arg, Phe (core, >2 kcal/mol each in
#   Ala-scanning, Bogan & Thorn 1998) plus Lys, Asp, Glu (salt bridges
#   and H-bond networks, Moreira et al. 2007). Their density predicts
#   which patches will form high-affinity interfaces.
_COMPOSITE_HYDROPHOBIC_WEIGHT: float = 0.30
_COMPOSITE_BFACTOR_WEIGHT: float     = 0.20
_COMPOSITE_GEOMETRY_WEIGHT: float    = 0.15
_COMPOSITE_SS_WEIGHT: float          = 0.20
_COMPOSITE_HOTSPOT_WEIGHT: float     = 0.15

# Hot spot residue sets: core residues (Bogan & Thorn 1998) contribute
# >2 kcal/mol each in Ala-scanning and are weighted 1.0. Extended set
# (Moreira et al. 2007) forms salt bridges / H-bond networks and is
# weighted 0.5 to reflect lower average energetic contribution.
_HOT_SPOT_CORE: frozenset = frozenset({"TRP", "TYR", "ARG", "PHE"})
_HOT_SPOT_EXTENDED: frozenset = frozenset({"LYS", "ASP", "GLU"})
_HOT_SPOT_AA: frozenset = _HOT_SPOT_CORE | _HOT_SPOT_EXTENDED

# Only count hot spot residues with RSA above this threshold. Buried
# Trp/Phe are already desolvated and cannot anchor a new interface.
_HOT_SPOT_RSA_GATE: float = 0.15

# Hydrophobic fraction cap: patches above this are aggregation-prone.
# Applied as min(h, cap) / cap so the score saturates at 1.0.
_HYDROPHOBIC_CAP: float = 0.6

# Secondary structure scores — used for continuous weighted average.
# Strand/helix-containing patches are strongly preferred for binder design;
# loops carry conformational risk.
_SS_SCORES: dict = {"strand": 1.00, "helix": 0.80, "loop": 0.20}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BACKBONE_ATOMS = ("N", "CA", "C", "O")


def _mean_bfactor_for_patch(patch_residues: list) -> float:
    """Return mean backbone B-factor across all residues in a patch.

    Averages over N, CA, C, O atoms that are present. Returns 0.0 if no
    backbone atoms are found (e.g. CA-only models).

    Args:
        patch_residues: List of Biopython Residue objects.

    Returns:
        Mean B-factor as a float rounded to 3 decimal places.
    """
    bfactors = []
    for residue in patch_residues:
        for atom_name in _BACKBONE_ATOMS:
            try:
                bfactors.append(residue[atom_name].get_bfactor())
            except KeyError:
                pass
    if not bfactors:
        return 0.0
    return round(float(np.mean(bfactors)), 3)


def _majority_ss(patch_residues: list, ss_map: dict) -> str:
    """Return plurality-vote secondary structure label for a patch.

    Tie-breaking priority: helix > strand > loop. This means that if helix
    and strand are tied at the maximum count, helix is returned.

    Args:
        patch_residues: List of Biopython Residue objects in this patch.
        ss_map: Dict mapping (chain_id, residue.get_id()) to
            "helix" | "strand" | "loop". Produced by assign_dssp().
            Missing keys default to "loop".

    Returns:
        One of "helix", "strand", or "loop".
    """
    # Lower tie-break value wins: helix=0, strand=1, loop=2
    tie_break = {"helix": 0, "strand": 1, "loop": 2}
    counts: Counter = Counter()

    for residue in patch_residues:
        # DSSP key format: (chain_id_str, residue.get_id())
        # residue.get_parent() is the Chain; .get_id() returns the chain letter.
        chain_letter = residue.get_parent().get_id()
        key = (chain_letter, residue.get_id())
        label = ss_map.get(key, "loop")
        counts[label] += 1

    if not counts:
        return "loop"

    max_count = max(counts.values())
    # All labels at the maximum count — resolve tie with priority order
    candidates = [label for label, cnt in counts.items() if cnt == max_count]
    return min(candidates, key=lambda x: tie_break[x])


def _continuous_ss_score(patch_residues: list, ss_map: dict) -> float:
    """Return continuous SS score as weighted average of per-residue labels.

    Instead of collapsing a mixed helix/strand patch to one label,
    computes: 1.0 * frac_strand + 0.8 * frac_helix + 0.2 * frac_loop.

    Args:
        patch_residues: List of Biopython Residue objects in this patch.
        ss_map: Dict mapping (chain_id, residue.get_id()) to
            "helix" | "strand" | "loop".

    Returns:
        Float score in [0.2, 1.0].
    """
    counts = {"helix": 0, "strand": 0, "loop": 0}
    for residue in patch_residues:
        chain_letter = residue.get_parent().get_id()
        key = (chain_letter, residue.get_id())
        label = ss_map.get(key, "loop")
        counts[label] += 1

    total = sum(counts.values())
    if total == 0:
        return 0.2

    return round(
        (_SS_SCORES["strand"] * counts["strand"]
         + _SS_SCORES["helix"] * counts["helix"]
         + _SS_SCORES["loop"] * counts["loop"]) / total,
        3,
    )


# ---------------------------------------------------------------------------
# Primary entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    pdb_path: Path,
    chain_id: str,
    progress_callback=None,
) -> Path:
    """Orchestrate full structural scoring pipeline.

    Reads pdb_path, runs SASA -> patch clustering -> per-patch scoring,
    writes results.csv to the same directory as pdb_path.

    Pipeline steps:
        1. Parse PDB (QUIET=True suppresses verbose warnings).
        2. Validate chain_id is present in model 0.
        3. Compute RSA via freesasa (compute_rsa).
        4. Filter to surface residues: standard AA, hetflag == ' ', RSA >= threshold.
        5. Raise if too few surface residues to form patches.
        6. Cluster surface residues (cluster_surface_residues).
        7. Raise if no patches produced.
        8. Compute chain-level B-factor scores (compute_bfactor_scores).
        9. Assign DSSP secondary structure (assign_dssp; empty dict = all loop).
        10. Build all-atom coordinate array for burial scoring.
        11. Score geometry for each patch (score_geometry).
        12. Normalize burial across all patches (normalize_burial_scores).
        13. Assemble CSV rows and write results.csv.

    Args:
        pdb_path: Path to the input PDB file. results.csv is written to the
            same directory (pdb_path.parent / "results.csv").
        chain_id: Single-character chain identifier (e.g. "A").
        progress_callback: Optional callable invoked at each major pipeline
            stage as progress_callback(stage: str, pct: int). Stage names are
            "parsing", "sasa", "patches", "scoring", "ranking". pct is a
            percentage in [0, 100] indicating approximate completion. Passing
            None (the default) disables all callbacks; existing callers are
            unaffected.

    Returns:
        Path to the written results.csv file.

    Raises:
        ValueError: If chain_id is not present in the PDB, or if too few
            surface residues exist to form patches, or if no patches are formed.
        FileNotFoundError: If pdb_path does not exist on disk.
    """
    pdb_path = Path(pdb_path)
    if not pdb_path.exists():
        raise FileNotFoundError(f"PDB file not found: {pdb_path}")

    def _emit(stage: str, pct: int) -> None:
        """Invoke progress_callback if set. Silently no-ops if callback is None."""
        if progress_callback is not None:
            progress_callback(stage, pct)

    # ------------------------------------------------------------------
    # Step 1: Parse structure — PDB or mmCIF depending on file extension
    # ------------------------------------------------------------------
    if pdb_path.suffix.lower() == ".cif":
        parser = MMCIFParser(QUIET=True)
    else:
        parser = PDBParser(QUIET=True)
    structure = parser.get_structure("target", str(pdb_path))
    model = structure[0]

    # ------------------------------------------------------------------
    # Step 2: Validate chain exists
    # ------------------------------------------------------------------
    available_chains = [chain.get_id() for chain in model.get_chains()]
    if chain_id not in available_chains:
        raise ValueError(
            f"Chain '{chain_id}' not found in structure. "
            f"Available chains: {', '.join(sorted(available_chains))}"
        )
    _emit("parsing", 10)

    # ------------------------------------------------------------------
    # Step 3: Compute RSA (requires freesasa; skips cleanly on Windows
    # without MSVC — tests that call this will SKIP on that platform)
    # ------------------------------------------------------------------
    rsa_map = compute_rsa(structure, chain_id)
    _emit("sasa", 30)

    # ------------------------------------------------------------------
    # Step 4: Filter to surface residues
    # ------------------------------------------------------------------
    chain = model[chain_id]
    surface_residues = []
    all_chain_residues = []

    for residue in chain.get_residues():
        # Skip HETATM (hetflag != ' ') and non-standard amino acids
        hetflag = residue.get_id()[0]
        if hetflag != " " or residue.resname not in STANDARD_AA:
            continue
        all_chain_residues.append(residue)

        # RSA key: (chain_id, str(sequence_number))
        rsa_key = (chain_id, str(residue.get_id()[1]))
        rsa_value = rsa_map.get(rsa_key, 0.0)
        if rsa_value >= SURFACE_RSA_THRESHOLD:
            surface_residues.append(residue)

    # ------------------------------------------------------------------
    # Step 5: Guard — need at least MIN_PATCH_SIZE surface residues
    # Import MIN_PATCH_SIZE from patches to stay consistent with that module
    # ------------------------------------------------------------------
    from scout.patches import MIN_PATCH_SIZE  # avoid circular at module level

    if len(surface_residues) < MIN_PATCH_SIZE:
        raise ValueError(
            f"Too few surface residues ({len(surface_residues)}) to form patches. "
            "Check chain selection or RSA threshold."
        )

    # ------------------------------------------------------------------
    # Step 6: Cluster surface residues into patches
    # ------------------------------------------------------------------
    patches = cluster_surface_residues(surface_residues)
    _emit("patches", 55)

    # ------------------------------------------------------------------
    # Step 7: Guard — patches must be non-empty
    # ------------------------------------------------------------------
    if not patches:
        raise ValueError(
            "No patches formed. The chain may be too small or fully buried."
        )

    # ------------------------------------------------------------------
    # Step 8: Chain-level B-factor scores (with pLDDT auto-detection)
    # ------------------------------------------------------------------
    plddt_detected = is_likely_plddt(all_chain_residues, pdb_path=str(pdb_path))
    if plddt_detected:
        logger.info("pLDDT detected in B-factor column — using direct mapping")
    bfactor_scores = compute_bfactor_scores(all_chain_residues, plddt_mode=plddt_detected)

    # ------------------------------------------------------------------
    # Step 9: DSSP secondary structure (falls back to empty dict = all loop)
    # ------------------------------------------------------------------
    ss_map = assign_dssp(model, str(pdb_path))

    # ------------------------------------------------------------------
    # Step 10: All-atom coordinate array for burial scoring
    # ------------------------------------------------------------------
    all_atom_coords_list = []
    for residue in all_chain_residues:
        for atom in residue.get_atoms():
            all_atom_coords_list.append(atom.get_vector().get_array())

    if all_atom_coords_list:
        all_atom_coords = np.array(all_atom_coords_list, dtype=float)
    else:
        # Empty fallback — score_geometry will return zero burial
        all_atom_coords = np.zeros((1, 3), dtype=float)

    # ------------------------------------------------------------------
    # Step 11: Score geometry for each patch
    # ------------------------------------------------------------------
    raw_scores = []
    for patch in patches:
        raw_scores.append(score_geometry(patch, all_atom_coords))

    # ------------------------------------------------------------------
    # Step 12: Normalize burial across all patches
    # ------------------------------------------------------------------
    normalize_burial_scores(raw_scores)
    _emit("scoring", 80)

    # ------------------------------------------------------------------
    # Step 13: Assemble rows and write results.csv
    # ------------------------------------------------------------------
    results_csv_path = pdb_path.parent / "results.csv"
    rows = []

    for patch_idx, (patch, score_dict) in enumerate(zip(patches, raw_scores)):
        # Residue string: "LYS23,ASP24" (resname + sequence number)
        residues_str = ",".join(
            f"{res.resname}{res.get_id()[1]}" for res in patch
        )

        # Mean RSA across patch residues
        rsa_values = []
        for res in patch:
            rsa_key = (chain_id, str(res.get_id()[1]))
            rsa_values.append(rsa_map.get(rsa_key, 0.0))
        mean_rsa = round(float(np.mean(rsa_values)) if rsa_values else 0.0, 3)

        # Mean bfactor_score across patch residues (using per-residue scores)
        # Outlier penalty: if any residue scores below 0.333 (z > 2.0 in
        # crystallographic mode, or pLDDT < 33 in pLDDT mode), the patch
        # mean is reduced by 0.15. One weak link in the backbone can
        # undermine the entire designed interface.
        patch_bfactor_scores = []
        for res in patch:
            full_id = res.get_full_id()
            if full_id in bfactor_scores:
                patch_bfactor_scores.append(bfactor_scores[full_id])
        bfactor_score = round(
            float(np.mean(patch_bfactor_scores)) if patch_bfactor_scores else 0.0, 3
        )
        if patch_bfactor_scores and min(patch_bfactor_scores) < 0.333:
            bfactor_score = round(max(0.0, bfactor_score - 0.15), 3)

        # Mean raw B-factor from backbone atoms
        mean_bfac = _mean_bfactor_for_patch(patch)

        # Secondary structure plurality vote (for display label)
        secondary_structure = _majority_ss(patch, ss_map)

        # Continuous SS score: weighted average of per-residue labels.
        # Mixed helix/strand patches score between 0.8-1.0 instead of
        # collapsing to a single categorical label.
        ss_score = _continuous_ss_score(patch, ss_map)

        # Hot spot density: core residues (Trp/Tyr/Arg/Phe) weight 1.0,
        # extended set (Lys/Asp/Glu) weight 0.5. Only counted if RSA >= 0.15
        # so buried hot-spot residues are excluded (already desolvated).
        hot_spot_weighted = 0.0
        for res in patch:
            if res.resname in _HOT_SPOT_AA:
                hs_rsa_key = (chain_id, str(res.get_id()[1]))
                if rsa_map.get(hs_rsa_key, 0.0) >= _HOT_SPOT_RSA_GATE:
                    if res.resname in _HOT_SPOT_CORE:
                        hot_spot_weighted += 1.0
                    else:
                        hot_spot_weighted += 0.5
        hot_spot_fraction = round(hot_spot_weighted / len(patch) if patch else 0.0, 3)

        # Hydrophobic exposure: capped hydrophobicity × mean_rsa.
        # Cap at 0.6 to prevent aggregation-prone all-nonpolar patches
        # from dominating rankings.
        hydrophobicity_capped = min(score_dict["hydrophobicity"], _HYDROPHOBIC_CAP) / _HYDROPHOBIC_CAP
        hydrophobic_exposure = round(hydrophobicity_capped * mean_rsa, 3)

        composite_score = round(
            _COMPOSITE_HYDROPHOBIC_WEIGHT * hydrophobic_exposure
            + _COMPOSITE_BFACTOR_WEIGHT   * bfactor_score
            + _COMPOSITE_GEOMETRY_WEIGHT  * score_dict["geometry_score"]
            + _COMPOSITE_SS_WEIGHT        * ss_score
            + _COMPOSITE_HOTSPOT_WEIGHT   * hot_spot_fraction,
            3,
        )

        # Patch Cb centroid for spatial separation during candidate selection
        cb_coords_patch = [get_cb_coord(res) for res in patch]
        cb_coords_patch = [c for c in cb_coords_patch if c is not None]
        if cb_coords_patch:
            centroid = np.mean(cb_coords_patch, axis=0)
        else:
            centroid = np.array([0.0, 0.0, 0.0])

        rows.append({
            "epitope_id": patch_idx + 1,
            "residues": residues_str,
            "residue_count": len(patch),
            "mean_rsa": mean_rsa,
            "composite_score": composite_score,
            "hydrophobic_exposure": hydrophobic_exposure,
            "hydrophobicity": score_dict["hydrophobicity"],
            "hot_spot_fraction": hot_spot_fraction,
            "geometry_score": score_dict["geometry_score"],
            "burial_raw": score_dict["burial_raw"],
            "mean_bfactor": mean_bfac,
            "bfactor_score": bfactor_score,
            "secondary_structure": secondary_structure,
            "centroid_x": round(float(centroid[0]), 2),
            "centroid_y": round(float(centroid[1]), 2),
            "centroid_z": round(float(centroid[2]), 2),
            "is_plddt": "1" if plddt_detected else "0",
        })

    # Sort by composite_score descending so the download CSV is ranked
    rows.sort(key=lambda r: r["composite_score"], reverse=True)

    with results_csv_path.open("w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(
        "Pipeline complete: %d patches written to %s",
        len(rows),
        results_csv_path,
    )
    _emit("ranking", 95)
    return results_csv_path


# ---------------------------------------------------------------------------
# Feasibility pipeline
# ---------------------------------------------------------------------------

FEASIBILITY_CSV_COLUMNS = [
    "epitope_id",
    "residues",
    "residue_count",
    "composite_feasibility",
    "tier",
    "surface_topology",
    "epitope_rigidity",
    "geometric_access",
    "glycan_risk",
    "interface_competition",
    "recommended_approach",
    "recommended_scaffold",
    "design_scale_min",
    "design_scale_max",
    "expected_hit_rate",
    "risk_factors",
]


def run_feasibility_pipeline(
    pdb_path: Path,
    chain_id: str,
    epitope_residues: list[int],
    progress_callback=None,
) -> Path:
    """Run feasibility assessment on a specific epitope region.

    This pipeline reuses parsing, RSA, B-factor, and DSSP from the main
    pipeline, then adds feasibility-specific scoring: surface topology,
    geometric accessibility, glycan proximity, prior binder precedent,
    and PPI interface competition.

    Args:
        pdb_path: Path to input PDB/mmCIF file.
        chain_id: Single-character chain identifier.
        epitope_residues: List of residue numbers defining the epitope.
        progress_callback: Optional callable(stage: str, pct: int).

    Returns:
        Path to feasibility_results.csv in the same directory as pdb_path.

    Raises:
        ValueError: If chain or residues are invalid.
        FileNotFoundError: If pdb_path does not exist.
    """
    from scout.accessibility import score_approach_cone
    from scout.feasibility import (
        classify_tier,
        compute_feasibility_score,
        generate_recommendations,
    )
    from scout.glycan import detect_glycosylation_sequons, score_glycan_proximity

    pdb_path = Path(pdb_path)
    if not pdb_path.exists():
        raise FileNotFoundError(f"PDB file not found: {pdb_path}")

    def _emit(stage: str, pct: int) -> None:
        if progress_callback is not None:
            progress_callback(stage, pct)

    # Step 1: Parse structure
    if pdb_path.suffix.lower() == ".cif":
        parser = MMCIFParser(QUIET=True)
    else:
        parser = PDBParser(QUIET=True)
    structure = parser.get_structure("target", str(pdb_path))
    model = structure[0]

    available_chains = [c.get_id() for c in model.get_chains()]
    if chain_id not in available_chains:
        raise ValueError(
            f"Chain '{chain_id}' not found. Available: {', '.join(sorted(available_chains))}"
        )
    chain = model[chain_id]
    _emit("parsing", 10)

    # Step 2: Compute RSA
    rsa_map = compute_rsa(structure, chain_id)
    _emit("sasa", 25)

    # Step 3: Collect chain residues and identify the epitope patch
    all_chain_residues = []
    patch_residues = []
    epitope_set = set(epitope_residues)

    for residue in chain.get_residues():
        hetflag = residue.get_id()[0]
        if hetflag != " " or residue.resname not in STANDARD_AA:
            continue
        all_chain_residues.append(residue)
        if residue.get_id()[1] in epitope_set:
            patch_residues.append(residue)

    if not patch_residues:
        raise ValueError(
            f"No valid residues found for epitope selection "
            f"(requested: {epitope_residues})"
        )

    # Build all-atom coordinate array
    all_atom_coords_list = []
    for residue in all_chain_residues:
        for atom in residue.get_atoms():
            all_atom_coords_list.append(atom.get_vector().get_array())
    all_atom_coords = np.array(all_atom_coords_list, dtype=float) if all_atom_coords_list else np.zeros((1, 3))

    # Patch centroid
    cb_coords = [get_cb_coord(r) for r in patch_residues]
    cb_coords = [c for c in cb_coords if c is not None]
    if not cb_coords:
        raise ValueError("No Cb/Ca coordinates found for epitope residues")
    patch_centroid = np.mean(cb_coords, axis=0)

    _emit("bfactor", 35)

    # Step 4: B-factor / rigidity scoring
    plddt_detected = is_likely_plddt(all_chain_residues, pdb_path=str(pdb_path))
    bfactor_scores = compute_bfactor_scores(all_chain_residues, plddt_mode=plddt_detected)

    patch_bf_scores = []
    for res in patch_residues:
        fid = res.get_full_id()
        if fid in bfactor_scores:
            patch_bf_scores.append(bfactor_scores[fid])
    rigidity_score = float(np.mean(patch_bf_scores)) if patch_bf_scores else 0.5

    # Step 5: Surface topology (burial-based concavity proxy)
    _emit("topology", 45)
    geo = score_geometry(patch_residues, all_atom_coords)
    burial_raw = geo["burial_raw"]

    # Compute max burial across all surface residues for normalization
    max_burial = burial_raw
    for residue in all_chain_residues:
        rsa_key = (chain_id, str(residue.get_id()[1]))
        if rsa_map.get(rsa_key, 0.0) >= SURFACE_RSA_THRESHOLD:
            single_geo = score_geometry([residue], all_atom_coords)
            if single_geo["burial_raw"] > max_burial:
                max_burial = single_geo["burial_raw"]

    topology_score = burial_raw / max_burial if max_burial > 0 else 0.5

    # Add curvature variance bonus: irregular surfaces provide grip
    dist_from_centroid = [np.linalg.norm(c - patch_centroid) for c in cb_coords]
    if len(dist_from_centroid) > 1:
        curvature_var = float(np.std(dist_from_centroid))
        curvature_bonus = min(curvature_var / 5.0, 0.15)
        topology_score = min(1.0, topology_score + curvature_bonus)

    # Step 6: Geometric accessibility
    _emit("accessibility", 55)
    access_score = score_approach_cone(
        patch_residues, all_atom_coords,
        patch_resnums=epitope_set, chain=chain,
    )

    # Step 7: Glycan risk
    _emit("glycan", 65)
    sequons = detect_glycosylation_sequons(chain)
    glycan_score = score_glycan_proximity(sequons, patch_centroid)

    # Step 8: Interface competition
    _emit("interfaces", 75)
    competition_score = 1.0  # default: no competition
    try:
        from scout.interfaces import detect_ppi_interfaces
        interfaces = detect_ppi_interfaces(model, chain_id)
        if interfaces:
            for iface in interfaces:
                contact_set = set(iface.get("contact_residues", []))
                overlap = epitope_set & contact_set
                if overlap:
                    overlap_frac = len(overlap) / len(epitope_set)
                    competition_score = max(0.1, 1.0 - overlap_frac)
                    break
    except Exception:
        logger.warning("PPI interface detection failed; using default score")

    # Step 10: Composite scoring and recommendations
    _emit("scoring", 95)
    dimensions = {
        "surface_topology": round(topology_score, 3),
        "epitope_rigidity": round(rigidity_score, 3),
        "geometric_access": round(access_score, 3),
        "glycan_risk": round(glycan_score, 3),
        "interface_competition": round(competition_score, 3),
    }

    composite = compute_feasibility_score(dimensions)
    tier, _ = classify_tier(composite)
    result = generate_recommendations(dimensions, composite, tier, len(patch_residues))

    # Residue string
    residues_str = ",".join(f"{r.resname}{r.get_id()[1]}" for r in patch_residues)

    # Write CSV
    feasibility_csv_path = pdb_path.parent / "feasibility_results.csv"
    row = {
        "epitope_id": 1,
        "residues": residues_str,
        "residue_count": len(patch_residues),
        "composite_feasibility": composite,
        "tier": result.tier,
        "surface_topology": dimensions["surface_topology"],
        "epitope_rigidity": dimensions["epitope_rigidity"],
        "geometric_access": dimensions["geometric_access"],
        "glycan_risk": dimensions["glycan_risk"],
        "interface_competition": dimensions["interface_competition"],
        "recommended_approach": result.recommended_approach,
        "recommended_scaffold": result.recommended_scaffold,
        "design_scale_min": result.design_scale_min,
        "design_scale_max": result.design_scale_max,
        "expected_hit_rate": result.expected_hit_rate,
        "risk_factors": "; ".join(result.risk_factors) if result.risk_factors else "None identified",
    }

    with feasibility_csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FEASIBILITY_CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(row)

    logger.info("Feasibility pipeline complete: tier=%s, score=%.3f", result.tier, composite)
    return feasibility_csv_path
