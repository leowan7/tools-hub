"""Composite developability scoring entry point.

Combines the three dimensions in :data:`_DIMENSIONS` into a single
developability profile with a 0-to-1 composite score and per-residue flags
suitable for downstream visualization.

EVERY FINDING IS DETECTED FROM THE SEQUENCE ALONE, so this module scores an
ordered sequence with no structure in hand, and that is the only mode any
caller in the request path uses today (no production call site passes
``residue_rsa``). A structure is OPTIONAL and does one thing: the
``residue_rsa`` argument, measured by
``shared/surface_patch.py::surface_profile``, raises the severity of liability
motifs found to be solvent-exposed (``dimensions/liabilities.py::_escalate``).
It changes no dimension's findings and no other dimension reads it. Without it
every motif keeps its chemistry-only base severity and reports
``exposed: None``.

The patch figure itself is not computed here; it lives in
``shared/surface_patch.py``.

Two dimensions this module shipped with are gone, both on purpose:

* **Human-likeness.** Leo ruled on 2026-11-16 that it will not be a Tools Hub
  feature. The dimension and its germline reference data were deleted rather
  than gated off, so nothing displayed can be derived from it.
* **Hydrophobicity (the sequence-window screen).** It slid an 11-residue
  Kyte-Doolittle window and counted windows over a threshold, which measures
  nothing about whether those residues are on the surface. Leo's ruling of the
  same date is that stickiness is a surface property only, so a hydrophobic
  stretch is reportable only once measured solvent-exposed. It was removed from
  the composite as well as from the page: the composite is displayed, so
  leaving it in would have kept a displayed number derived from the screen.

The Kyte-Doolittle scale that screen used went with it
(``shared/developability/data/kyte_doolittle.py``). The replacement figure in
``shared/surface_patch.py`` does not need it: it classifies APOLAR ATOMS by
element (carbon and sulfur), the usual definition of the apolar surface, rather
than scoring residues on a hydropathy scale.
"""

from __future__ import annotations

from typing import Dict, List

from shared.developability.dimensions.aggregation import score_aggregation
from shared.developability.dimensions.charge import score_charge
from shared.developability.dimensions.liabilities import score_liabilities

VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")

# Equal weights across dimensions, derived rather than written out so the
# three stay equal and keep summing to 1.0 if the set ever changes again.
_DIMENSIONS: tuple[str, ...] = ("liabilities", "charge", "aggregation")

# Exposed here so callers (or a future Flask wrapper) can tune them without
# touching the dimension modules.
DEFAULT_WEIGHTS: Dict[str, float] = {
    name: 1 / len(_DIMENSIONS) for name in _DIMENSIONS
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
    aggregation_values: List[float],
) -> List[dict]:
    """Assemble a per-residue annotation array for frontend rendering.

    Carries no ``hydropathy`` key: it held the smoothed Kyte-Doolittle value
    from the deleted sequence-window screen (see the module docstring). No
    template ever rendered it.

    Args:
        sequence: Validated amino acid string.
        liability_records: Output from ``score_liabilities``.
        aggregation_values: Smoothed per-residue a3v values.

    Returns:
        List of dicts keyed by 1-based position with residue, aggregation,
        and any liability annotations on that residue.
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
            "aggregation": aggregation_values[i] if i < len(aggregation_values) else 0.0,
            "liabilities": liabilities_by_position.get(pos, []),
        }
        flags.append(entry)
    return flags


def score_developability(
    sequence: str,
    chain_type: str = "VH",
    weights: Dict[str, float] = None,
    residue_rsa=None,
) -> dict:
    """Score a single antibody-variable-region sequence for developability.

    Args:
        sequence: Amino acid sequence (one-letter code).
        chain_type: Chain class, echoed back on the result as a LABEL only.
            It affects no number here: its two former consumers were the
            deleted human-likeness dimension and the CDR span heuristic that
            the liability severity rule replaced with measured exposure.
        residue_rsa: Optional per-residue relative solvent accessibility for
            ``sequence``, in sequence order, from
            ``shared/surface_patch.py::surface_profile``. Supplying it raises
            the severity of liability motifs measured solvent-exposed. Without
            it every motif keeps its chemistry-only base severity and reports
            ``exposed: None``.
        weights: Optional override of dimension weights. Keys must be a
            subset of :data:`_DIMENSIONS` and values must sum to 1.0.

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

    dimensions = {
        "liabilities": score_liabilities(cleaned, residue_rsa=residue_rsa),
        "charge": score_charge(cleaned),
        "aggregation": score_aggregation(cleaned),
    }
    assert set(dimensions) == set(_DIMENSIONS), (
        "dimensions computed here must match _DIMENSIONS, which is what "
        "DEFAULT_WEIGHTS and the weights-override validation are keyed on"
    )

    composite = sum(
        used_weights[name] * result["score"] for name, result in dimensions.items()
    )

    per_residue_flags = _per_residue_flags(
        cleaned,
        dimensions["liabilities"]["liabilities"],
        dimensions["aggregation"]["per_residue"],
    )

    return {
        "sequence": cleaned,
        "chain_type": chain_type.upper(),
        "length": len(cleaned),
        "dimensions": dimensions,
        "weights": used_weights,
        "composite_score": round(composite, 4),
        "per_residue_flags": per_residue_flags,
    }
