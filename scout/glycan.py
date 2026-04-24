"""Glycosylation sequon detection and proximity scoring for feasibility assessment.

N-linked glycosylation occurs at Asn residues in the NxS/T motif (where x is
not Pro). Glycan chains near a binder epitope can sterically occlude the
binding interface, reducing hit rates in de novo design campaigns.

This module detects sequon positions on a target chain and scores how close
they are to a candidate epitope patch. Closer glycans = higher risk = lower
feasibility score.

Exports:
    detect_glycosylation_sequons  -- find NxS/T motifs on a chain
    score_glycan_proximity        -- 0-1 score based on nearest sequon distance
"""

from __future__ import annotations

import logging

import numpy as np

from scout.patches import get_cb_coord

logger = logging.getLogger(__name__)

# Standard one-letter to three-letter mapping for sequon detection
_THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def detect_glycosylation_sequons(chain) -> list[dict]:
    """Find N-linked glycosylation sequons (N-x-S/T, x != P) on a chain.

    Args:
        chain: A Biopython Chain object.

    Returns:
        List of dicts, each with keys:
            resnum   -- int, residue number of the Asn
            resname  -- str, three-letter code ("ASN")
            cb_coord -- np.ndarray (3,) or None if Cb/Ca missing
            motif    -- str, e.g. "N-K-T" showing the tripeptide
    """
    standard_residues = [
        r for r in chain.get_residues()
        if r.id[0] == " " and r.resname in _THREE_TO_ONE
    ]

    sequons = []
    for i in range(len(standard_residues) - 2):
        r0, r1, r2 = standard_residues[i], standard_residues[i + 1], standard_residues[i + 2]
        aa0 = _THREE_TO_ONE.get(r0.resname, "?")
        aa1 = _THREE_TO_ONE.get(r1.resname, "?")
        aa2 = _THREE_TO_ONE.get(r2.resname, "?")

        if aa0 == "N" and aa1 != "P" and aa2 in ("S", "T"):
            sequons.append({
                "resnum": r0.id[1],
                "resname": r0.resname,
                "cb_coord": get_cb_coord(r0),
                "motif": f"{aa0}-{aa1}-{aa2}",
            })

    logger.info("Found %d N-linked glycosylation sequon(s) on chain %s",
                len(sequons), chain.id)
    return sequons


def score_glycan_proximity(
    sequons: list[dict],
    patch_centroid: np.ndarray,
    max_dist: float = 20.0,
    min_dist: float = 5.0,
) -> float:
    """Score glycan risk based on proximity of nearest sequon to the epitope.

    Returns 1.0 (no risk) if no sequons exist or all are beyond max_dist.
    Returns 0.0 (high risk) if a sequon is at or closer than min_dist.
    Linear interpolation between min_dist and max_dist.

    Args:
        sequons: Output of detect_glycosylation_sequons().
        patch_centroid: (3,) array, centroid of the epitope patch.
        max_dist: Distance in Angstroms beyond which glycans pose no risk.
        min_dist: Distance at which glycan risk is maximal (score = 0).

    Returns:
        Float in [0.0, 1.0]. Higher = less glycan risk = better feasibility.
    """
    if not sequons:
        return 1.0

    coords = [s["cb_coord"] for s in sequons if s["cb_coord"] is not None]
    if not coords:
        return 1.0

    distances = [np.linalg.norm(c - patch_centroid) for c in coords]
    nearest = min(distances)

    if nearest >= max_dist:
        return 1.0
    if nearest <= min_dist:
        return 0.0

    return (nearest - min_dist) / (max_dist - min_dist)
