"""Binder feasibility scoring engine.

Computes a composite feasibility score across six dimensions to predict
how difficult it will be to design a de novo binder against a given
epitope region. Produces a difficulty tier and specific recommendations
for design approach, scaffold type, and campaign scale.

Dimensions (weighted):
    surface_topology     (0.20) — concavity/convexity of the epitope
    epitope_rigidity     (0.20) — B-factor or pLDDT stability
    geometric_access     (0.20) — approach cone openness
    glycan_risk          (0.15) — N-linked glycosylation proximity
    prior_precedent      (0.15) — known binders in SAbDab/RCSB
    interface_competition(0.10) — natural PPI overlap

Exports:
    DIMENSION_WEIGHTS         -- dict of dimension name -> weight
    TIER_THRESHOLDS           -- dict of tier name -> minimum score
    compute_feasibility_score -- composite scoring function
    classify_tier             -- map composite score to difficulty tier
    generate_recommendations  -- produce actionable guidance
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring weights — must sum to 1.0
# ---------------------------------------------------------------------------

DIMENSION_WEIGHTS: dict[str, float] = {
    "surface_topology": 0.25,
    "epitope_rigidity": 0.25,
    "geometric_access": 0.25,
    "glycan_risk": 0.15,
    "interface_competition": 0.10,
}

# ---------------------------------------------------------------------------
# Tier classification thresholds
# ---------------------------------------------------------------------------

TIER_THRESHOLDS: list[tuple[str, float]] = [
    ("Straightforward", 0.70),
    ("Moderate", 0.50),
    ("Challenging", 0.35),
    ("High risk", 0.0),
]

# ---------------------------------------------------------------------------
# Dimension labels for display
# ---------------------------------------------------------------------------

DIMENSION_LABELS: dict[str, str] = {
    "surface_topology": "Surface topology",
    "epitope_rigidity": "Epitope rigidity",
    "geometric_access": "Geometric accessibility",
    "glycan_risk": "Glycan risk",
    "interface_competition": "Natural PPI competition",
}

DIMENSION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "surface_topology": {
        "high": "Concave pocket or groove provides good geometric complementarity for a binder scaffold.",
        "mid": "Mixed surface topology. Moderate geometric features for scaffold engagement.",
        "low": "Flat or convex surface with minimal features. Binder scaffolds lack geometric anchoring.",
    },
    "epitope_rigidity": {
        "high": "Rigid, well-ordered epitope. Predictable conformation aids computational design.",
        "mid": "Moderate flexibility. Design should account for some conformational variation.",
        "low": "Highly flexible or disordered region. Predicted structures may not reflect the binding conformation.",
    },
    "geometric_access": {
        "high": "Epitope is fully exposed. Binder scaffolds can approach from multiple angles.",
        "mid": "Partially occluded. Limited approach directions constrain scaffold topology.",
        "low": "Deeply buried or sterically blocked. Only extended or very small scaffolds can reach.",
    },
    "glycan_risk": {
        "high": "No N-linked glycosylation sites near the epitope.",
        "mid": "Glycosylation site(s) within 20 Angstroms. May partially occlude the epitope in vivo.",
        "low": "Glycosylation site(s) very close to epitope. High risk of steric occlusion in vivo.",
    },
    "interface_competition": {
        "high": "No endogenous binding partner contacts this epitope in the uploaded structure. A designed binder would not need to compete with a natural interaction.",
        "mid": "Partial overlap with an endogenous binding partner interface in the uploaded structure. A designed binder may need to outcompete this natural interaction.",
        "low": "Epitope sits within a major endogenous PPI interface in the uploaded structure. A designed binder must outcompete the natural binding partner.",
    },
}


@dataclass
class FeasibilityResult:
    """Complete feasibility assessment for a single epitope."""

    dimensions: dict[str, float] = field(default_factory=dict)
    composite_score: float = 0.0
    tier: str = "Unknown"
    tier_color: str = "#888"
    dimension_descriptions: dict[str, str] = field(default_factory=dict)
    recommended_approach: str = ""
    recommended_scaffold: str = ""
    design_scale_min: int = 0
    design_scale_max: int = 0
    expected_hit_rate: str = ""
    hit_rate_citation: str = ""
    risk_factors: list[str] = field(default_factory=list)


def compute_feasibility_score(dimensions: dict[str, float]) -> float:
    """Compute weighted composite feasibility score.

    Args:
        dimensions: Dict mapping dimension name to score (0-1).

    Returns:
        Float in [0.0, 1.0], weighted composite.
    """
    total = 0.0
    for dim, weight in DIMENSION_WEIGHTS.items():
        total += dimensions.get(dim, 0.0) * weight
    return round(total, 3)


def classify_tier(composite_score: float) -> tuple[str, str]:
    """Map composite score to difficulty tier and color.

    Returns:
        (tier_name, hex_color) tuple.
    """
    for tier_name, threshold in TIER_THRESHOLDS:
        if composite_score >= threshold:
            colors = {
                "Straightforward": "#2B9E7E",
                "Moderate": "#D4A843",
                "Challenging": "#D47843",
                "High risk": "#D44343",
            }
            return tier_name, colors.get(tier_name, "#888")
    return "High risk", "#D44343"


def _get_dimension_description(dim: str, score: float) -> str:
    """Return the appropriate description for a dimension score."""
    descs = DIMENSION_DESCRIPTIONS.get(dim, {})
    if score >= 0.65:
        return descs.get("high", "")
    elif score >= 0.35:
        return descs.get("mid", "")
    else:
        return descs.get("low", "")


def _identify_risk_factors(dimensions: dict[str, float]) -> list[str]:
    """Identify specific risk factors from low-scoring dimensions."""
    risks = []
    if dimensions.get("surface_topology", 1.0) < 0.35:
        risks.append("Flat or convex epitope surface reduces geometric complementarity")
    if dimensions.get("epitope_rigidity", 1.0) < 0.35:
        risks.append("Flexible or disordered epitope region may not match predicted conformation")
    if dimensions.get("geometric_access", 1.0) < 0.35:
        risks.append("Epitope is sterically occluded, limiting scaffold approach angles")
    if dimensions.get("glycan_risk", 1.0) < 0.50:
        risks.append("N-linked glycosylation sites near the epitope may block binding in vivo")
    if dimensions.get("interface_competition", 1.0) < 0.50:
        risks.append("Epitope overlaps a natural protein-protein interface")
    return risks


def generate_recommendations(
    dimensions: dict[str, float],
    composite_score: float,
    tier: str,
    epitope_size: int,
) -> FeasibilityResult:
    """Generate a complete feasibility assessment with recommendations.

    Args:
        dimensions: Dict of dimension name -> score (0-1).
        composite_score: Weighted composite score.
        tier: Difficulty tier string.
        epitope_size: Number of residues in the epitope patch.

    Returns:
        FeasibilityResult with all fields populated.
    """
    tier_name, tier_color = classify_tier(composite_score)

    # Scaffold recommendation
    topology = dimensions.get("surface_topology", 0.5)
    if topology >= 0.60 and epitope_size <= 8:
        scaffold = "Miniprotein (<60 aa). Concave pocket and compact epitope favor ultra-small scaffolds."
    elif topology >= 0.40:
        scaffold = "Nanobody (VHH). Versatile single-domain format suits moderate surface topology."
    else:
        scaffold = "Custom scaffold or extended loop design. Flat surface requires non-standard approach geometry."

    # Design scale recommendation — numbers grounded in published data
    # (design_min, design_max, hit_rate_str, citation_str)
    scale_map = {
        "Straightforward": (
            5_000, 10_000, "5-15%",
            "Watson et al. 2023 (Nature 620:1089): 19% hit rate across 5 targets with RFdiffusion. "
            "Pacesa et al. 2025 (Nature): BindCraft 10-100% per target (avg 46%, n=212). "
            "Adaptyv BenchBB 2025: RFdiffusion 12.6% on standardized benchmark."
        ),
        "Moderate": (
            10_000, 30_000, "2-8%",
            "Bennett et al. 2023 (Nat Commun 14:2625): 1-5% with Rosetta+AF2 filtering. "
            "Adaptyv EGFR competition 2025: 2.5% (5/201 designs). "
            "AlphaProteo (Zambaldi et al. 2024): 9% on harder targets."
        ),
        "Challenging": (
            30_000, 50_000, "1-5%",
            "Cao et al. 2022 (Nature 605:551): <1% from 15K-100K designs per target with Rosetta. "
            "Modern tools (RFdiffusion+AF2 filtering) improve this ~10x (Bennett et al. 2023)."
        ),
        "High risk": (
            50_000, 100_000, "<2%",
            "Cao et al. 2022: some targets required 100K designs for <10 hits. "
            "AlphaProteo 2024: 0% on TNFa (flat homotrimer). "
            "Flat/convex surfaces consistently underperform (Kang et al. 2024, PMC11092582)."
        ),
    }
    scale_min, scale_max, hit_rate, hit_rate_cite = scale_map.get(
        tier_name, (30_000, 50_000, "1-5%", "")
    )

    # Approach recommendation — based on biophysical feasibility, not novelty
    if composite_score >= 0.70:
        approach = "De novo design using RFdiffusion, BindCraft, and Boltzgen in parallel. Favorable biophysics support a focused campaign with standard design parameters."
    elif composite_score >= 0.50:
        approach = "De novo design with broad exploration across multiple backbone topologies and hotspot combinations. Increase sampling diversity to compensate for moderate target difficulty."
    elif composite_score >= 0.35:
        approach = "De novo design with extended sampling (30,000+ designs). Consider multiple epitope regions in parallel. Scaffold topology may need to be non-standard to accommodate target geometry."
    else:
        approach = "Consider hybrid approach: de novo design for initial scaffold discovery, followed by library-based affinity maturation. Alternatively, evaluate whether a different epitope on this target would be more tractable."

    # Per-dimension descriptions
    dim_descs = {dim: _get_dimension_description(dim, score) for dim, score in dimensions.items()}

    return FeasibilityResult(
        dimensions=dimensions,
        composite_score=composite_score,
        tier=tier_name,
        tier_color=tier_color,
        dimension_descriptions=dim_descs,
        recommended_approach=approach,
        recommended_scaffold=scaffold,
        design_scale_min=scale_min,
        design_scale_max=scale_max,
        expected_hit_rate=hit_rate,
        hit_rate_citation=hit_rate_cite,
        risk_factors=_identify_risk_factors(dimensions),
    )
