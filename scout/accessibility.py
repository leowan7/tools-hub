"""Geometric approach cone analysis for binder feasibility assessment.

A binder scaffold (~60-80 residues) needs physical space to approach and
engage an epitope. If the epitope is in a deep pocket, narrow groove, or
surrounded by protein mass, the fraction of viable approach directions is
reduced, making design harder.

This module estimates how sterically accessible an epitope is by sampling
directions from the patch centroid and checking for target atom occlusion.

Exports:
    score_approach_cone  -- 0-1 score based on fraction of clear approach angles
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.spatial import KDTree

from scout.patches import get_cb_coord

logger = logging.getLogger(__name__)

# Probe radius: how far from the centroid to check for obstructions.
# 25 Angstroms approximates the radius of a compact binder scaffold.
_PROBE_RADIUS = 25.0

# Minimum distance from centroid to consider an atom as obstructing.
# Atoms closer than this are part of the epitope itself.
_INNER_RADIUS = 6.0


def _fibonacci_hemisphere(n: int, center_of_mass: np.ndarray, centroid: np.ndarray) -> np.ndarray:
    """Generate n approximately uniform directions on the outward hemisphere.

    The hemisphere faces away from the protein center of mass, which is
    the relevant set of approach directions for an external binder.

    Args:
        n: Number of sample directions.
        center_of_mass: (3,) array, center of mass of the full chain.
        centroid: (3,) array, centroid of the epitope patch.

    Returns:
        (m, 3) array of unit direction vectors on the outward hemisphere,
        where m <= n (directions pointing inward are filtered).
    """
    outward = centroid - center_of_mass
    outward_norm = np.linalg.norm(outward)
    if outward_norm < 1e-6:
        outward = np.array([0.0, 0.0, 1.0])
    else:
        outward = outward / outward_norm

    golden_ratio = (1 + np.sqrt(5)) / 2
    indices = np.arange(n)
    theta = np.arccos(1 - 2 * (indices + 0.5) / (2 * n))
    phi = 2 * np.pi * indices / golden_ratio

    directions = np.column_stack([
        np.sin(theta) * np.cos(phi),
        np.sin(theta) * np.sin(phi),
        np.cos(theta),
    ])

    dots = directions @ outward
    hemisphere = directions[dots > 0]

    if len(hemisphere) == 0:
        return directions[:max(1, n // 4)]

    return hemisphere


def score_approach_cone(
    patch_residues: list,
    all_chain_atoms: np.ndarray,
    patch_resnums: set[int] | None = None,
    chain=None,
    n_samples: int = 100,
) -> float:
    """Score geometric accessibility of an epitope patch.

    Samples directions on the outward hemisphere from the patch centroid
    and checks what fraction are unobstructed by target atoms within the
    probe radius.

    Args:
        patch_residues: List of Biopython Residue objects in the patch.
        all_chain_atoms: (N, 3) numpy array of all chain heavy-atom coords.
        patch_resnums: Set of residue numbers in the patch (to exclude from
            obstruction checks). If None, derived from patch_residues.
        chain: Biopython Chain object (used for center of mass). If None,
            center of mass is estimated from all_chain_atoms.
        n_samples: Number of hemisphere directions to sample.

    Returns:
        Float in [0.0, 1.0]. Higher = more accessible = better feasibility.
    """
    cb_coords = [get_cb_coord(r) for r in patch_residues]
    cb_coords = [c for c in cb_coords if c is not None]
    if not cb_coords:
        return 0.5

    centroid = np.mean(cb_coords, axis=0)

    if chain is not None:
        com_coords = []
        for r in chain.get_residues():
            if r.id[0] != " ":
                continue
            for a in r.get_atoms():
                com_coords.append(a.get_vector().get_array())
        if com_coords:
            center_of_mass = np.mean(com_coords, axis=0)
        else:
            center_of_mass = np.mean(all_chain_atoms, axis=0)
    else:
        center_of_mass = np.mean(all_chain_atoms, axis=0)

    if patch_resnums is None:
        patch_resnums = {r.id[1] for r in patch_residues}

    non_patch_atoms = []
    if chain is not None:
        for r in chain.get_residues():
            if r.id[0] != " " or r.id[1] in patch_resnums:
                continue
            for a in r.get_atoms():
                non_patch_atoms.append(a.get_vector().get_array())
    if not non_patch_atoms:
        non_patch_atoms = all_chain_atoms

    non_patch_coords = np.array(non_patch_atoms)
    if len(non_patch_coords) == 0:
        return 1.0

    tree = KDTree(non_patch_coords)

    directions = _fibonacci_hemisphere(n_samples, center_of_mass, centroid)
    if len(directions) == 0:
        return 0.5

    clear_count = 0
    for d in directions:
        n_steps = 4
        obstructed = False
        for step in range(1, n_steps + 1):
            probe_point = centroid + d * (_INNER_RADIUS + (_PROBE_RADIUS - _INNER_RADIUS) * step / n_steps)
            nearby = tree.query_ball_point(probe_point, r=4.0)
            if nearby:
                obstructed = True
                break
        if not obstructed:
            clear_count += 1

    score = clear_count / len(directions)
    logger.info("Approach cone: %d/%d directions clear (score=%.2f)",
                clear_count, len(directions), score)
    return score
