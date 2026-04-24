"""Quality flag computation for Epitope Scout epitope patches.

Flags are informational risk indicators for binder design campaigns.
They do NOT modify composite_score — they are advisory annotations only.
"""

import re

# Import CSV_COLUMNS from pipeline. On Windows without freesasa, pipeline.py
# fails to import at module level (freesasa not available without MSVC).
# To keep this module importable on all platforms, we duplicate the column list
# here and import the pipeline version at runtime inside functions that need it.
# CSV_COLUMNS must stay in sync with analysis.pipeline.CSV_COLUMNS — single
# source of truth is pipeline.py; update both if the column list changes.
_CSV_COLUMNS_BASE = [
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

# Annotated CSV column order — used for both top-3 and full CSV download paths.
# Single source of truth: never duplicate this list in app.py.
CSV_COLUMNS_ANNOTATED = _CSV_COLUMNS_BASE + ["quality_flags"]

# Thresholds — calibrated against actual pipeline output distributions.
# Values below each cutoff trigger the corresponding flag.
# See RESEARCH.md "Pitfall 5" for calibration guidance.
_HYDROPHOBICITY_POLAR_CUTOFF: float = 0.20   # below = all-polar patch
_BURIAL_CONVEX_CUTOFF: float = 5.0            # below = fully convex surface
_BFACTOR_FLEXIBLE_CUTOFF: float = 0.35        # below = high-flexibility region
_TERMINAL_PROXIMITY: int = 5                  # residues from N/C terminus
_TERMINAL_FRACTION: float = 0.50              # >50% terminal = flag

# Positive and negative residue sets for electrostatic asymmetry
_POSITIVE_AA: frozenset = frozenset({"ARG", "LYS", "HIS"})
_NEGATIVE_AA: frozenset = frozenset({"ASP", "GLU"})
_CHARGE_ASYMMETRY_CUTOFF: float = 0.40  # absolute difference in fractions

# Glycosylation sequon pattern: N-x-S/T where x != P
# Matched against 3-residue windows from the residue name list.
_GLYCAN_SEQUON_RESIDUES: frozenset = frozenset({"ASN"})
_GLYCAN_FOLLOW_RESIDUES: frozenset = frozenset({"SER", "THR"})


def _parse_residues_str(residues_str: str) -> list[tuple[str, int]]:
    """Parse 'LYS23,ASP24' into [('LYS', 23), ('ASP', 24)]."""
    pairs = []
    for token in residues_str.split(","):
        token = token.strip()
        match = re.match(r"([A-Z]{3})(-?\d+)", token)
        if match:
            pairs.append((match.group(1), int(match.group(2))))
    return pairs


def compute_quality_flags(
    secondary_structure: str,
    hydrophobicity: float,
    burial_raw: float,
    bfactor_score: float,
    is_functional_site: bool,
    residues_str: str = "",
    chain_length: int = 0,
    is_plddt: bool = False,
) -> str:
    """Return pipe-delimited quality flag string for a patch.

    Flags are informational — they do NOT modify composite_score.
    Returns empty string if no flags apply.

    Args:
        secondary_structure: One of "helix", "strand", or "loop".
        hydrophobicity: Normalized hydrophobicity score (0-1) from pipeline.
        burial_raw: Heavy atom count within 8 Ang of patch centroid.
        bfactor_score: Normalized, inverted B-factor score (0-1).
        is_functional_site: True if patch overlaps a literature-returned region.
        residues_str: Comma-separated residue string (e.g. "LYS23,ASP24").
        chain_length: Total residue count of the target chain.
        is_plddt: True if B-factor column contains AlphaFold pLDDT values.

    Returns:
        Pipe-delimited flag string, e.g. "loop-only anchor|all-polar patch".
        Empty string if no flags apply.
    """
    flags = []

    if secondary_structure == "loop":
        flags.append("loop-only anchor")

    if hydrophobicity < _HYDROPHOBICITY_POLAR_CUTOFF:
        flags.append("all-polar patch")

    if burial_raw < _BURIAL_CONVEX_CUTOFF:
        flags.append("fully convex surface")

    if is_functional_site:
        flags.append("known functional site")

    if bfactor_score < _BFACTOR_FLEXIBLE_CUTOFF:
        flags.append("high-flexibility region")

    # -- New v3.1 flags --------------------------------------------------

    if is_plddt:
        flags.append("B-factor unreliable (pLDDT)")

    # Parse residue names and numbers for remaining flags
    parsed = _parse_residues_str(residues_str) if residues_str else []
    res_names = [name for name, _ in parsed]
    res_nums = [num for _, num in parsed]

    # Terminal patch: >50% of residues within 5 positions of chain ends
    if res_nums and chain_length > 0:
        min_resnum = min(res_nums)
        max_resnum = min_resnum + chain_length - 1
        terminal_count = sum(
            1 for num in res_nums
            if num <= min_resnum + _TERMINAL_PROXIMITY
            or num >= max_resnum - _TERMINAL_PROXIMITY
        )
        if terminal_count / len(res_nums) > _TERMINAL_FRACTION:
            flags.append("terminal patch")

    # Electrostatic asymmetry: large imbalance between +/- residues
    if len(res_names) >= 3:
        n_total = len(res_names)
        n_pos = sum(1 for r in res_names if r in _POSITIVE_AA)
        n_neg = sum(1 for r in res_names if r in _NEGATIVE_AA)
        frac_pos = n_pos / n_total
        frac_neg = n_neg / n_total
        if abs(frac_pos - frac_neg) > _CHARGE_ASYMMETRY_CUTOFF:
            flags.append("electrostatic asymmetry")

    # Glycan proximity: patch contains Asn that could be part of N-x-S/T sequon
    # This is approximate — we check if ASN is present and SER/THR is nearby
    if len(parsed) >= 2:
        asn_nums = {num for name, num in parsed if name == "ASN"}
        st_nums = {num for name, num in parsed if name in _GLYCAN_FOLLOW_RESIDUES}
        for asn_num in asn_nums:
            # Check if SER/THR is at position +2 (N-x-S/T sequon)
            if (asn_num + 2) in st_nums:
                flags.append("glycan proximity")
                break

    return "|".join(flags)
