"""Theoretical isoelectric point and net charge scoring.

Uses Biopython's ProteinAnalysis for pI and net charge at pH 7.4.
Scores are based on distance from published tolerable ranges for clinical
monoclonal antibodies.
"""

from __future__ import annotations

from Bio.SeqUtils.ProtParam import ProteinAnalysis

VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")

# Ranges drawn from published Mab developability literature
# (e.g. Jain et al. 2017 PNAS survey of clinical Mabs).
PI_IDEAL_LOW = 6.0
PI_IDEAL_HIGH = 9.0
CHARGE_IDEAL_LOW = -5.0
CHARGE_IDEAL_HIGH = 20.0

# Outside these tolerance widths, score decays to zero.
PI_TOLERANCE = 2.0
CHARGE_TOLERANCE = 10.0


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


def _range_score(value: float, low: float, high: float, tolerance: float) -> float:
    """Score a scalar against an ideal range with linear decay outside.

    Inside [low, high] returns 1.0. Outside, decays linearly to 0.0 over
    ``tolerance`` units.

    Args:
        value: Measured scalar.
        low: Lower bound of ideal range.
        high: Upper bound of ideal range.
        tolerance: Width of linear decay outside the ideal range.

    Returns:
        Score in [0, 1].
    """
    if low <= value <= high:
        return 1.0
    if value < low:
        distance = low - value
    else:
        distance = value - high
    return max(0.0, 1.0 - distance / tolerance)


def score_charge(sequence: str) -> dict:
    """Compute pI, net charge at pH 7.4, and combined charge score.

    Args:
        sequence: Protein sequence.

    Returns:
        Dictionary with ``pI``, ``net_charge_7_4``, ``pI_score``,
        ``charge_score``, and combined ``score`` (mean of both).
    """
    cleaned = _validate_sequence(sequence)
    analysis = ProteinAnalysis(cleaned)
    pI = float(analysis.isoelectric_point())
    net_charge = float(analysis.charge_at_pH(7.4))

    pI_score = _range_score(pI, PI_IDEAL_LOW, PI_IDEAL_HIGH, PI_TOLERANCE)
    charge_score = _range_score(
        net_charge, CHARGE_IDEAL_LOW, CHARGE_IDEAL_HIGH, CHARGE_TOLERANCE
    )

    combined = (pI_score + charge_score) / 2.0

    return {
        "pI": round(pI, 2),
        "net_charge_7_4": round(net_charge, 2),
        "pI_score": round(pI_score, 4),
        "charge_score": round(charge_score, 4),
        "score": round(combined, 4),
    }
