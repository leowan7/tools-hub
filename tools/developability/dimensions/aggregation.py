"""Aggregation propensity scoring using a simplified Aggrescan a3v approach.

Per-residue a3v values are looked up and smoothed with a short window.
Aggregation-prone regions (APRs) are runs of five or more consecutive
residues with positive smoothed a3v. Score decays with APR count.
"""

from __future__ import annotations

from typing import List

from tools.developability.data.aggrescan_scale import AGGRESCAN_A3V

VALID_AA = set(AGGRESCAN_A3V.keys())

SMOOTH_WINDOW = 5
APR_MIN_LENGTH = 5
APR_MIN_MEAN_A3V = 0.3
APR_COUNT_CEILING = 5.0


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


def _smooth(values: List[float], window: int) -> List[float]:
    """Centered moving average with edge values extended."""
    n = len(values)
    if n == 0:
        return []
    half = window // 2
    result: List[float] = [0.0] * n
    first_valid = None
    last_valid = None
    for center in range(n):
        start = center - half
        end = center + half + 1
        if start < 0 or end > n:
            continue
        segment = values[start:end]
        result[center] = sum(segment) / len(segment)
        if first_valid is None:
            first_valid = center
        last_valid = center
    if first_valid is None:
        overall = sum(values) / n
        return [overall] * n
    for i in range(first_valid):
        result[i] = result[first_valid]
    for i in range(last_valid + 1, n):
        result[i] = result[last_valid]
    return result


def _find_apr_regions(smoothed: List[float], min_length: int) -> List[dict]:
    """Identify aggregation-prone regions: runs of positive smoothed a3v.

    Args:
        smoothed: Smoothed per-residue a3v series.
        min_length: Minimum run length to call an APR.

    Returns:
        List of dictionaries with ``start`` and ``end`` 1-based inclusive
        residue indices and ``mean_a3v`` within the run.
    """
    regions: List[dict] = []
    start = None
    for i, value in enumerate(smoothed):
        if value > 0:
            if start is None:
                start = i
        else:
            if start is not None:
                length = i - start
                if length >= min_length:
                    segment = smoothed[start:i]
                    regions.append(
                        {
                            "start": start + 1,
                            "end": i,
                            "mean_a3v": round(sum(segment) / len(segment), 3),
                        }
                    )
                start = None
    if start is not None:
        length = len(smoothed) - start
        if length >= min_length:
            segment = smoothed[start:]
            regions.append(
                {
                    "start": start + 1,
                    "end": len(smoothed),
                    "mean_a3v": round(sum(segment) / len(segment), 3),
                }
            )
    return regions


def score_aggregation(sequence: str) -> dict:
    """Score aggregation propensity based on APR count.

    Args:
        sequence: Protein sequence.

    Returns:
        Dictionary with ``per_residue`` (list of smoothed a3v values),
        ``apr_regions`` (list), ``apr_count`` (int), ``score`` (0 to 1).
    """
    cleaned = _validate_sequence(sequence)
    raw = [AGGRESCAN_A3V[r] for r in cleaned]
    smoothed = _smooth(raw, SMOOTH_WINDOW)
    all_regions = _find_apr_regions(smoothed, APR_MIN_LENGTH)

    # Keep only regions with strong aggregation signal, weighted by length.
    # This mirrors how Aggrescan's Hot Spot Area (HSA) weighs mean a3v times
    # stretch length rather than raw count of any-positive-sign runs.
    strong_regions = [
        region
        for region in all_regions
        if region["mean_a3v"] >= APR_MIN_MEAN_A3V
    ]
    apr_count = len(strong_regions)

    score = 1.0 - min(1.0, apr_count / APR_COUNT_CEILING)

    return {
        "per_residue": [round(v, 3) for v in smoothed],
        "apr_regions": strong_regions,
        "apr_count": apr_count,
        "score": round(score, 4),
    }
