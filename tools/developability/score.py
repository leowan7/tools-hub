"""Composite developability scoring entry point.

Combines five independent scientific dimensions into a single developability
profile with a 0-to-1 composite score and per-residue flags suitable for
downstream visualization.
"""

from __future__ import annotations

from typing import Dict, List

from tools.developability.dimensions.aggregation import score_aggregation
from tools.developability.dimensions.charge import score_charge
from tools.developability.dimensions.humanness import score_humanness
from tools.developability.dimensions.hydrophobicity import score_hydrophobicity
from tools.developability.dimensions.liabilities import score_liabilities

VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")

# Equal weights across dimensions. Exposed here so callers (or future
# Flask wrapper) can tune them without touching the dimension modules.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "humanness": 0.2,
    "liabilities": 0.2,
    "charge": 0.2,
    "hydrophobicity": 0.2,
    "aggregation": 0.2,
}


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


def _per_residue_flags(
    sequence: str,
    liability_records: List[dict],
    hydrophobicity_values: List[float],
    aggregation_values: List[float],
) -> List[dict]:
    """Assemble a per-residue annotation array for frontend rendering.

    Args:
        sequence: Validated amino acid string.
        liability_records: Output from ``score_liabilities``.
        hydrophobicity_values: Smoothed per-residue Kyte-Doolittle values.
        aggregation_values: Smoothed per-residue a3v values.

    Returns:
        List of dicts keyed by 1-based position with residue, hydropathy,
        aggregation, and any liability annotations on that residue.
    """
    length = len(sequence)
    liabilities_by_position: Dict[int, List[dict]] = {}
    for record in liability_records:
        liabilities_by_position.setdefault(record["position"], []).append(record)

    flags: List[dict] = []
    for i in range(length):
        pos = i + 1
        entry = {
            "position": pos,
            "residue": sequence[i],
            "hydropathy": hydrophobicity_values[i] if i < len(hydrophobicity_values) else 0.0,
            "aggregation": aggregation_values[i] if i < len(aggregation_values) else 0.0,
            "liabilities": liabilities_by_position.get(pos, []),
        }
        flags.append(entry)
    return flags


def score_developability(
    sequence: str,
    chain_type: str = "VH",
    weights: Dict[str, float] = None,
) -> dict:
    """Score a single antibody-variable-region sequence for developability.

    Args:
        sequence: Amino acid sequence (one-letter code).
        chain_type: Chain class. Accepted: "VH", "VK", "VL". Affects
            germline pool for humanness and CDR boundary heuristic for
            liability severity.
        weights: Optional override of dimension weights. Keys must be a
            subset of {"humanness", "liabilities", "charge", "hydrophobicity",
            "aggregation"} and values must sum to 1.0.

    Returns:
        A nested dictionary with ``sequence``, ``chain_type``,
        ``dimensions`` (per-dimension outputs), ``composite_score``,
        ``weights`` used, and ``per_residue_flags``.

    Raises:
        ValueError: If the sequence is empty/invalid or weights are malformed.
    """
    cleaned = _validate_sequence(sequence)

    used_weights: Dict[str, float] = dict(DEFAULT_WEIGHTS)
    if weights is not None:
        if not isinstance(weights, dict):
            raise ValueError("weights must be a dict")
        unknown = set(weights) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"unknown weight keys: {sorted(unknown)}")
        used_weights.update(weights)
        total = sum(used_weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0 (got {total:.4f})")

    humanness_result = score_humanness(cleaned, chain_type=chain_type)
    liabilities_result = score_liabilities(cleaned, chain_type=chain_type)
    charge_result = score_charge(cleaned)
    hydrophobicity_result = score_hydrophobicity(cleaned)
    aggregation_result = score_aggregation(cleaned)

    composite = (
        used_weights["humanness"] * humanness_result["score"]
        + used_weights["liabilities"] * liabilities_result["score"]
        + used_weights["charge"] * charge_result["score"]
        + used_weights["hydrophobicity"] * hydrophobicity_result["score"]
        + used_weights["aggregation"] * aggregation_result["score"]
    )

    per_residue_flags = _per_residue_flags(
        cleaned,
        liabilities_result["liabilities"],
        hydrophobicity_result["per_residue"],
        aggregation_result["per_residue"],
    )

    return {
        "sequence": cleaned,
        "chain_type": chain_type.upper(),
        "length": len(cleaned),
        "dimensions": {
            "humanness": humanness_result,
            "liabilities": liabilities_result,
            "charge": charge_result,
            "hydrophobicity": hydrophobicity_result,
            "aggregation": aggregation_result,
        },
        "weights": used_weights,
        "composite_score": round(composite, 4),
        "per_residue_flags": per_residue_flags,
    }
