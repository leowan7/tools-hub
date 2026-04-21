"""Hydrophobic patch detection using the Kyte-Doolittle scale.

A sliding window of 11 residues averages per-residue hydropathy.
Windows with mean hydropathy above +1.5 are flagged as strong
hydrophobic patches. Score decays with the number of distinct patches.
"""

from __future__ import annotations

from typing import List

from tools.developability.data.kyte_doolittle import KYTE_DOOLITTLE

VALID_AA = set(KYTE_DOOLITTLE.keys())

WINDOW_SIZE = 11
PATCH_THRESHOLD = 1.5


def _validate_sequence(sequence: str) -> str:
    """Validate and uppercase an amino acid sequence."""
    if not isinstance(sequence, str):
        raise ValueError("sequence must be a string")
    cleaned = sequence.strip().upper()
    if not cleaned:
        raise ValueError("sequence must be non-empty")
    bad = set(cleaned) - VALID_AA
    if bad:
        raise ValueError(
            f"sequence contains non-canonical residues: {sorted(bad)}"
        )
    return cleaned


def _window_means(values: List[float], window: int) -> List[float]:
    """Compute centered sliding window means of a numeric series.

    The returned list has the same length as ``values``; indices too close
    to the edges to form a full window carry the value of the nearest valid
    window center.

    Args:
        values: Per-residue numeric series.
        window: Window size (must be odd and >= 1).

    Returns:
        Smoothed series of the same length.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    n = len(values)
    if n == 0:
        return []
    half = window // 2
    means: List[float] = [0.0] * n
    # Compute valid centered means; fill edges with nearest available value.
    first_valid = None
    last_valid = None
    for center in range(n):
        start = center - half
        end = center + half + 1
        if start < 0 or end > n:
            continue
        segment = values[start:end]
        means[center] = sum(segment) / len(segment)
        if first_valid is None:
            first_valid = center
        last_valid = center
    if first_valid is None:
        # Sequence shorter than window: use full-sequence mean everywhere.
        overall = sum(values) / n
        return [overall] * n
    for i in range(first_valid):
        means[i] = means[first_valid]
    for i in range(last_valid + 1, n):
        means[i] = means[last_valid]
    return means


def _count_patches(smoothed: List[float], threshold: float) -> int:
    """Count distinct contiguous runs where the smoothed signal is above threshold.

    Args:
        smoothed: Smoothed per-residue hydropathy values.
        threshold: Threshold above which a residue is considered patch-like.

    Returns:
        Integer count of non-overlapping patches.
    """
    patch_count = 0
    in_patch = False
    for value in smoothed:
        if value > threshold:
            if not in_patch:
                patch_count += 1
                in_patch = True
        else:
            in_patch = False
    return patch_count


def score_hydrophobicity(sequence: str) -> dict:
    """Compute hydrophobic patch count and score.

    Args:
        sequence: Protein sequence.

    Returns:
        Dictionary with ``per_residue`` (list of smoothed hydropathy values),
        ``patch_count`` (int), and ``score`` (0 to 1, higher is better).
    """
    cleaned = _validate_sequence(sequence)
    per_residue_raw = [KYTE_DOOLITTLE[r] for r in cleaned]
    smoothed = _window_means(per_residue_raw, WINDOW_SIZE)
    patch_count = _count_patches(smoothed, PATCH_THRESHOLD)

    # Three or more strong patches drops to zero.
    score = 1.0 - min(1.0, patch_count / 3.0)

    return {
        "per_residue": [round(v, 3) for v in smoothed],
        "patch_count": patch_count,
        "score": round(score, 4),
    }
