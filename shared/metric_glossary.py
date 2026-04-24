"""Single source of truth for metric definitions shown in the candidate table.

Each entry maps a score key (as it appears in ``job.result.candidates[].scores``)
to a display label, one-sentence definition, the "good" range, and the primary
citation. Referenced by the ``candidate_table.html`` macro and the export routes.
"""

from __future__ import annotations

from typing import Optional

GLOSSARY: dict[str, dict] = {
    "ipTM": {
        "label": "ipTM",
        "definition": (
            "Interface predicted Template Modeling score. Measures structural "
            "confidence at the binder–target interface specifically (0–1 scale)."
        ),
        "good_range": "> 0.75 strong; > 0.65 acceptable",
        "citation": "Evans et al., Science 2021 (AlphaFold-Multimer)",
    },
    "pLDDT": {
        "label": "pLDDT",
        "definition": (
            "Predicted Local Distance Difference Test. Per-residue confidence "
            "in the modelled structure on a 0–100 scale."
        ),
        "good_range": "> 80 very high confidence; 60–80 acceptable",
        "citation": "Jumper et al., Nature 2021 (AlphaFold2)",
    },
    "pAE": {
        "label": "pAE (Å)",
        "definition": (
            "Predicted Aligned Error. Expected positional error (Å) between two "
            "residues after optimal alignment. Low cross-interface pAE indicates "
            "confident complex geometry."
        ),
        "good_range": "< 5 Å across the interface",
        "citation": "Evans et al., Science 2021 (AlphaFold-Multimer)",
    },
    "pTM": {
        "label": "pTM",
        "definition": (
            "Predicted Template Modeling score. Global structural confidence "
            "across the entire complex (0–1 scale)."
        ),
        "good_range": "> 0.7 strong; > 0.5 acceptable",
        "citation": "Jumper et al., Nature 2021 (AlphaFold2)",
    },
    "refolding_rmsd": {
        "label": "Refolding RMSD (Å)",
        "definition": (
            "Cα RMSD between the designed binder and the same sequence refolded "
            "independently by AlphaFold2. Low values confirm the binder is "
            "self-consistent — it will fold to the intended backbone."
        ),
        "good_range": "< 1.5 Å; < 1.0 Å excellent",
        "citation": "Bennett et al., Nat Commun 2023 (BindCraft)",
    },
    "RMSD": {
        "label": "RMSD (Å)",
        "definition": (
            "Root Mean Square Deviation of Cα atoms between the designed binder "
            "and a reference scaffold or template."
        ),
        "good_range": "Context-dependent; lower is closer to the input scaffold",
        "citation": "",
    },
    "shape_complementarity": {
        "label": "Shape complementarity (SC)",
        "definition": (
            "Lawrence & Colman shape complementarity index. Measures geometric "
            "fit between the binder and target at the interface surface (0–1 scale)."
        ),
        "good_range": "> 0.65 good; > 0.75 excellent (antibody–antigen avg ~0.64)",
        "citation": "Lawrence & Colman, J Mol Biol 1993",
    },
    "SAP": {
        "label": "SAP score",
        "definition": (
            "Spatial Aggregation Propensity. Predicts hydrophobic patch exposure "
            "that correlates with aggregation risk during biomanufacturing."
        ),
        "good_range": "< 5 favourable; > 10 developability concern",
        "citation": "Chennamsetty et al., PNAS 2009",
    },
    "filter_status": {
        "label": "Filter",
        "definition": (
            "Pipeline quality gate result. Indicates whether the design passed "
            "post-diffusion validation (AF2-IG re-scoring, RMSD, SC thresholds). "
            "A 'stub' value means the silent-fallback path was triggered — "
            "do not trust those numbers."
        ),
        "good_range": "passed",
        "citation": "",
    },
}

# Display format per metric (Python format spec applied to the float value).
# "str" means no numeric conversion — render as-is.
_FORMAT: dict[str, str] = {
    "ipTM": ".3f",
    "pLDDT": ".1f",
    "pAE": ".2f",
    "pTM": ".3f",
    "refolding_rmsd": ".2f",
    "RMSD": ".2f",
    "shape_complementarity": ".3f",
    "SAP": ".2f",
    "filter_status": "str",
}


def get(metric_key: str) -> dict:
    """Return the glossary entry for ``metric_key``, or a generic fallback."""
    return GLOSSARY.get(
        metric_key,
        {
            "label": metric_key,
            "definition": "No definition available for this metric.",
            "good_range": "—",
            "citation": "",
        },
    )


def format_value(metric_key: str, raw) -> str:
    """Format ``raw`` for display using the metric's defined precision.

    Returns '—' for None/missing. Never raises.
    """
    if raw is None:
        return "—"
    fmt = _FORMAT.get(metric_key, ".3f")
    if fmt == "str":
        return str(raw) if raw else "—"
    try:
        return format(float(raw), fmt)
    except (TypeError, ValueError):
        return str(raw) if raw is not None else "—"


def to_json_safe() -> dict:
    """Return the glossary in a JSON-serialisable form for template injection."""
    return {
        k: {
            "label": v["label"],
            "definition": v["definition"],
            "good_range": v["good_range"],
            "citation": v["citation"],
        }
        for k, v in GLOSSARY.items()
    }
