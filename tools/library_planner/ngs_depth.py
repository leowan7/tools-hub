"""NGS read-depth planning using Poisson coverage statistics.

For a library of ``L`` unique members sampled uniformly at random with ``R``
reads, the probability that any given variant is observed at least once is
``1 - exp(-R/L)``. Solving for R at a target coverage fraction ``p``:

    R = -L * ln(1 - p)

This is the standard lander-Waterman / Poisson coverage formula and assumes
independent uniform sampling. Real NGS has uneven depth from PCR jackpotting
and sequencing bias; a 1.3-1.5x safety factor is conventional in production.

For sort campaigns the input library and each round's output pool each need
their own coverage budget. Round outputs are smaller (by the sort gate
fraction) so their read requirement scales down accordingly.
"""

from __future__ import annotations

import math
from typing import List


def _validate_library_size(library_size: int) -> None:
    """Validate library size is a positive int-like number."""
    if not isinstance(library_size, (int, float)):
        raise ValueError("library_size must be numeric")
    if isinstance(library_size, bool):
        raise ValueError("library_size must be numeric")
    if library_size < 1:
        raise ValueError("library_size must be >= 1")


def _validate_coverage(target_coverage: float) -> None:
    """Validate coverage is a fraction in (0, 1)."""
    if not isinstance(target_coverage, (int, float)):
        raise ValueError("target_coverage must be numeric")
    if target_coverage <= 0 or target_coverage >= 1:
        raise ValueError("target_coverage must be in (0, 1)")


def reads_for_coverage(library_size: int, target_coverage: float = 0.95) -> int:
    """Return the number of reads needed to reach a given Poisson coverage.

    Args:
        library_size: Number of unique variants in the pool.
        target_coverage: Fraction of variants the user wants observed at
            least once. Default 0.95.

    Returns:
        Integer read count. Rounded up to the nearest integer.
    """
    _validate_library_size(library_size)
    _validate_coverage(target_coverage)
    reads = -library_size * math.log(1.0 - target_coverage)
    return int(math.ceil(reads))


def coverage_profile(library_size: int) -> dict:
    """Return a standard set of Poisson coverage targets.

    Args:
        library_size: Number of unique variants in the pool.

    Returns:
        Dict with ``reads_for_50pct``, ``reads_for_95pct``,
        ``reads_for_99pct``, and ``recommended`` (95% target).
    """
    _validate_library_size(library_size)
    return {
        "reads_for_50pct": reads_for_coverage(library_size, 0.50),
        "reads_for_95pct": reads_for_coverage(library_size, 0.95),
        "reads_for_99pct": reads_for_coverage(library_size, 0.99),
        "recommended": reads_for_coverage(library_size, 0.95),
    }


def per_round_coverage(
    initial_library_size: int,
    round_gate_fractions: List[float],
    target_coverage: float = 0.95,
) -> List[dict]:
    """Return per-round NGS read requirements for a sort campaign.

    Each round's output pool is the previous pool times that round's gate
    fraction. The per-round coverage calculation assumes each round collapses
    the library to the top ``gate_fraction`` of cells.

    Args:
        initial_library_size: Starting library size (unique variants).
        round_gate_fractions: Fraction of cells kept at each round's gate.
            Length of this list sets the number of rounds.
        target_coverage: Fraction of variants to observe at each round.

    Returns:
        List of dicts, one per round, with ``round``, ``pool_size``, and
        ``recommended_reads``.
    """
    _validate_library_size(initial_library_size)
    _validate_coverage(target_coverage)
    if not isinstance(round_gate_fractions, list) or not round_gate_fractions:
        raise ValueError("round_gate_fractions must be a non-empty list")
    for gate in round_gate_fractions:
        if not isinstance(gate, (int, float)):
            raise ValueError("gate fractions must be numeric")
        if gate <= 0 or gate >= 1:
            raise ValueError("gate fractions must be in (0, 1)")

    per_round: List[dict] = []
    pool = float(initial_library_size)
    for idx, gate in enumerate(round_gate_fractions, start=1):
        pool = max(pool * gate, 1.0)
        pool_int = int(math.ceil(pool))
        per_round.append({
            "round": idx,
            "pool_size": pool_int,
            "recommended_reads": reads_for_coverage(
                pool_int, target_coverage=target_coverage
            ),
        })
    return per_round
