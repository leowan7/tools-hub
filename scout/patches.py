"""Spatial patch clustering for epitope surface residues (STRUCT-02).

Groups solvent-exposed residues into ~5-residue spatial patches using
Cb-Cb distance adjacency and BFS connected components. Patches represent
candidate epitope regions for binder design hotspot input.

Algorithm overview:
1. Build Cb coordinate array (Ca fallback for GLY; skip residues missing both).
2. KDTree query_pairs at CB_DISTANCE_CUTOFF Angstroms → adjacency graph.
3. BFS to find connected components.
4. Normalize component sizes:
   - Small (< MIN_PATCH_SIZE): merge into nearest neighbor component.
   - Large (> 2 * TARGET_PATCH_SIZE): greedy split into TARGET_PATCH_SIZE sub-patches.
5. Return list of lists of Biopython Residue objects.

Constants are exported so downstream consumers (scoring, reporting) can
import rather than redefine the thresholds.

Exports:
    CB_DISTANCE_CUTOFF    -- float, Angstrom radius for Cb-Cb adjacency
    TARGET_PATCH_SIZE     -- int, target residues per patch
    MIN_PATCH_SIZE        -- int, minimum patch size before merging
    get_cb_coord          -- helper: return Cb (or Ca for GLY) coordinate
    cluster_surface_residues -- primary entry point
"""

from __future__ import annotations

from collections import deque

import numpy as np
# scipy.spatial.KDTree is imported lazily inside cluster_surface_residues()
# to reduce worker startup memory — see sasa.py for the same pattern.

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Cb-Cb distance cutoff for declaring two residues spatially adjacent.
# 9 Å covers approximately one helical turn width, a standard choice for
# patch-based epitope mapping (Liang et al. 1998; Connolly surface literature).
CB_DISTANCE_CUTOFF: float = 9.0

# Maximum sequence separation (in residue numbers) for two residues to be
# considered part of the same patch. In multi-domain proteins (e.g. EGFR),
# domains fold back against each other, placing residues 200+ positions apart
# within 9 Å Cβ distance. These cross-domain contacts are poor epitope
# candidates because the domains may move relative to each other. 30 residues
# allows connections across loops and short insertions while preventing
# cross-domain patches.
MAX_SEQUENCE_SEPARATION: int = 30

# Target number of residues per patch. Chosen to match BindCraft hotspot
# input size (~5 residues per hotspot region).
TARGET_PATCH_SIZE: int = 5

# Patches smaller than this are considered too small to score meaningfully
# and are merged into the nearest neighbor patch.
MIN_PATCH_SIZE: int = 3


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def get_cb_coord(residue) -> np.ndarray | None:
    """Return the Cb coordinate for a residue; use Ca for glycine.

    Glycine has no side chain and therefore no Cb atom. Ca is used as a
    positional proxy. Residues missing both atoms return None and will be
    excluded from patch clustering.

    Args:
        residue: A Biopython Residue object.

    Returns:
        A (3,) float numpy array of the coordinate, or None if neither
        Cb nor Ca is present in the residue.
    """
    atom_name = "CA" if residue.resname == "GLY" else "CB"
    try:
        return residue[atom_name].get_vector().get_array()
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# Primary entry point
# ---------------------------------------------------------------------------

def cluster_surface_residues(surface_residues: list) -> list[list]:
    """Cluster surface residues into spatially contiguous patches.

    Uses KDTree Cb-Cb adjacency at CB_DISTANCE_CUTOFF Angstroms and BFS
    connected components to group residues. Component sizes are normalized
    to be near TARGET_PATCH_SIZE: small components are merged into the
    nearest neighbor; large components are greedily split.

    Args:
        surface_residues: List of Biopython Residue objects. Typically the
            output of filtering a chain's residues by RSA >= threshold.

    Returns:
        A list of lists, where each inner list is a patch containing one or
        more Biopython Residue objects. Returns an empty list if no residues
        have valid Cb/Ca coordinates.
    """
    if not surface_residues:
        return []

    # ------------------------------------------------------------------
    # Step A: Build coordinate array; track which residues have valid coords
    # ------------------------------------------------------------------
    valid_residues = []
    coord_list = []

    for res in surface_residues:
        coord = get_cb_coord(res)
        if coord is not None:
            valid_residues.append(res)
            coord_list.append(coord)

    if not valid_residues:
        return []

    coord_array = np.array(coord_list, dtype=float)  # shape (N, 3)

    # ------------------------------------------------------------------
    # Step B: KDTree adjacency at CB_DISTANCE_CUTOFF
    # ------------------------------------------------------------------
    from scipy.spatial import KDTree  # noqa: PLC0415 — deferred to reduce worker startup memory

    tree = KDTree(coord_array)
    # query_pairs returns a set of (i, j) index pairs where dist <= radius
    pairs = tree.query_pairs(CB_DISTANCE_CUTOFF)

    # Extract sequence numbers for sequence separation filter.
    # Residue.get_id() returns (hetflag, seqnum, icode).
    seq_nums = [res.get_id()[1] for res in valid_residues]

    # Build bidirectional adjacency dict: index -> set of neighbor indices.
    # Only connect residues that are both spatially close AND within
    # MAX_SEQUENCE_SEPARATION in sequence — prevents cross-domain patches
    # in multi-domain proteins like EGFR.
    adjacency: dict[int, set] = {idx: set() for idx in range(len(valid_residues))}
    for idx_i, idx_j in pairs:
        if abs(seq_nums[idx_i] - seq_nums[idx_j]) <= MAX_SEQUENCE_SEPARATION:
            adjacency[idx_i].add(idx_j)
            adjacency[idx_j].add(idx_i)

    # ------------------------------------------------------------------
    # Step C: BFS connected components
    # ------------------------------------------------------------------
    visited = set()
    components: list[list[int]] = []

    for start_idx in range(len(valid_residues)):
        if start_idx in visited:
            continue
        # BFS from this unvisited node
        component: list[int] = []
        queue = deque([start_idx])
        visited.add(start_idx)
        while queue:
            current = queue.popleft()
            component.append(current)
            for neighbor in adjacency[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        components.append(component)

    # ------------------------------------------------------------------
    # Step D: Size normalization
    # ------------------------------------------------------------------
    # Split large components first, then merge small ones.
    components = _split_large_components(components, coord_array)
    components = _merge_small_components(components, coord_array)

    # ------------------------------------------------------------------
    # Step E: Convert index lists to Residue lists
    # ------------------------------------------------------------------
    return [[valid_residues[idx] for idx in component] for component in components]


# ---------------------------------------------------------------------------
# Internal helpers for size normalization
# ---------------------------------------------------------------------------

def _split_large_components(
    components: list[list[int]],
    coord_array: np.ndarray,
) -> list[list[int]]:
    """Split components larger than 2 * TARGET_PATCH_SIZE into sub-patches.

    Uses a greedy nearest-neighbor seeding strategy: seed with the first
    residue, greedily add the TARGET_PATCH_SIZE-1 spatially nearest
    neighbors from within the component, then repeat on the remainder.

    Args:
        components: List of index lists (one per connected component).
        coord_array: (N, 3) coordinate array aligned to valid_residues.

    Returns:
        New list of index lists after splitting.
    """
    result: list[list[int]] = []
    large_threshold = 2 * TARGET_PATCH_SIZE

    for component in components:
        if len(component) <= large_threshold:
            result.append(component)
            continue

        # Greedy split: work on a mutable copy
        remaining = list(component)
        while len(remaining) > large_threshold:
            # Seed with the first element; find its nearest TARGET_PATCH_SIZE-1
            # neighbors within the remaining set by Cb-Cb distance.
            seed_idx = remaining[0]
            seed_coord = coord_array[seed_idx]

            # Compute distances from seed to all other remaining residues
            other_indices = remaining[1:]
            if not other_indices:
                break

            other_coords = coord_array[other_indices]
            distances = np.linalg.norm(other_coords - seed_coord, axis=1)
            sorted_order = np.argsort(distances)

            # Build sub-patch: seed + nearest TARGET_PATCH_SIZE-1 others
            sub_patch_size = min(TARGET_PATCH_SIZE, len(remaining))
            nearest_others = [other_indices[k] for k in sorted_order[: sub_patch_size - 1]]
            sub_patch = [seed_idx] + nearest_others

            result.append(sub_patch)
            # Remove assigned indices from remaining
            assigned = set(sub_patch)
            remaining = [idx for idx in remaining if idx not in assigned]

        if remaining:
            result.append(remaining)

    return result


def _merge_small_components(
    components: list[list[int]],
    coord_array: np.ndarray,
) -> list[list[int]]:
    """Merge components smaller than MIN_PATCH_SIZE into nearest neighbor.

    For each small component, find the nearest other component by minimum
    Cb-Cb distance and merge the small component into it.

    Args:
        components: List of index lists (one per component).
        coord_array: (N, 3) coordinate array aligned to valid_residues.

    Returns:
        New list of index lists after merging. If only one component exists
        or all are small, returns them as-is (cannot merge into nothing).
    """
    if len(components) <= 1:
        return components

    # Separate small components from normal-sized ones
    small: list[int] = []   # indices into `components` that are too small
    normal: list[int] = []  # indices into `components` that are large enough

    for comp_idx, component in enumerate(components):
        if len(component) < MIN_PATCH_SIZE:
            small.append(comp_idx)
        else:
            normal.append(comp_idx)

    # If everything is small (edge case: very few surface residues), return as-is
    if not normal:
        return components

    # Build a mutable list of lists we can extend
    merged: list[list[int]] = [list(components[idx]) for idx in normal]

    for small_comp_idx in small:
        small_component = components[small_comp_idx]
        small_coords = coord_array[small_component]  # shape (k, 3)

        # Find the nearest merged component by min pairwise Cb-Cb distance
        best_target_idx = 0
        best_min_dist = float("inf")

        for target_idx, target_component in enumerate(merged):
            target_coords = coord_array[target_component]  # shape (m, 3)
            # Compute all pairwise distances between small and target component
            # small_coords: (k, 3), target_coords: (m, 3)
            # Broadcasting: expand dims to (k, 1, 3) and (1, m, 3)
            dists = np.linalg.norm(
                small_coords[:, np.newaxis, :] - target_coords[np.newaxis, :, :],
                axis=2,
            )
            min_dist = dists.min()
            if min_dist < best_min_dist:
                best_min_dist = min_dist
                best_target_idx = target_idx

        # Merge the small component into the nearest normal component
        merged[best_target_idx].extend(small_component)

    return merged
