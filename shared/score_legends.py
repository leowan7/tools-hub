"""Per-tool score legends for the candidate-table column headers.

Where ``shared.metric_glossary`` answers "what is this metric called and
where did it come from?", this module answers a narrower question with
sharper thresholds: "given THIS tool's outputs, what counts as a good
column value, and what counts as excellent?".

Lookup is keyed on ``(tool_slug, column_key)`` so the same metric label
can carry different thresholds across tools (e.g. ipTM means slightly
different things between a BindCraft AF2-IG re-score and a Boltz-2
calibrated cofold). When no tool-specific entry exists, the helper
returns ``None`` and the candidate_table macro falls back to the generic
``metric_glossary`` tooltip.

The macro reads ``score_legends_for(tool_slug)`` via a Jinja global
registered in ``app.py``; templates do not import this module directly.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class Legend(TypedDict):
    good: float
    excellent: float
    direction: str  # "higher_is_better" or "lower_is_better"
    explanation: str


# (tool_slug, column_key) -> Legend.
# The column_key matches the score key in candidate.scores[...] (the same
# keys the candidate_table macro iterates over). Numeric thresholds are
# chosen to be defensible across the major papers, not aspirational.
SCORE_LEGENDS: dict[tuple[str, str], Legend] = {
    # ── ProteinMPNN (sequence design) ─────────────────────────────────
    ("mpnn", "recovery"): {
        "good": 0.4,
        "excellent": 0.6,
        "direction": "higher_is_better",
        "explanation": (
            "Recovery is the fraction of native residues recovered. "
            "Above 0.4 is a usable design; above 0.6 is excellent."
        ),
    },
    ("mpnn", "score"): {
        "good": 1.2,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "MPNN negative log likelihood per residue. Lower means the "
            "designed sequence is more confident on the input backbone. "
            "Below 1.2 is usable; below 1.0 is excellent."
        ),
    },

    # ── AlphaFold2 (single-prediction fold) ───────────────────────────
    ("af2", "plddt"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence. Above 80 is confidently folded; "
            "above 90 is high confidence."
        ),
    },
    ("af2", "ptm"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "pTM is global fold confidence. Above 0.7 is a credible "
            "model; above 0.8 is strong."
        ),
    },
    ("af2", "iptm"): {
        "good": 0.6,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM predicts complex confidence. Above 0.6 is a "
            "plausible interface; above 0.75 is strong."
        ),
    },

    # ── ColabFold and ESMFold reuse AF2-style pLDDT scale ────────────
    ("colabfold", "plddt"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence. Above 80 is confidently folded; "
            "above 90 is high confidence."
        ),
    },
    ("colabfold", "ptm"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "pTM is global fold confidence. Above 0.7 is a credible "
            "model; above 0.8 is strong."
        ),
    },
    ("colabfold", "iptm"): {
        "good": 0.6,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM predicts complex confidence. Above 0.6 is a "
            "plausible interface; above 0.75 is strong."
        ),
    },
    ("esmfold", "plddt"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is ESMFold confidence (AF2 scale). Above 80 is "
            "confidently folded; above 90 is high confidence."
        ),
    },

    # ── RFdiffusion (backbone diffusion + MPNN + AF2 multimer rescore)
    ("rfdiffusion", "ipTM"): {
        "good": 0.65,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from the AF2 multimer re-score. Above 0.65 "
            "is a credible binder; above 0.75 is strong."
        ),
    },
    ("rfdiffusion", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence on the designed binder. Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    ("rfdiffusion", "i_pAE"): {
        "good": 10.0,
        "excellent": 6.0,
        "direction": "lower_is_better",
        "explanation": (
            "Interface pAE is AF2's expected positional error across "
            "the binder-target interface. Below 10 angstroms passes; "
            "below 6 is strong."
        ),
    },
    ("rfdiffusion", "RMSD"): {
        "good": 1.5,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "RMSD against the design target. Below 1.5 angstroms is "
            "good agreement; below 1.0 is excellent."
        ),
    },

    # ── BindCraft (free hallucination + AF2-IG filters) ──────────────
    ("bindcraft", "ipTM"): {
        "good": 0.75,
        "excellent": 0.85,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM predicts binding likelihood. Above 0.75 is "
            "a credible binder; above 0.85 is a strong candidate."
        ),
    },
    ("bindcraft", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence on the designed binder. Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    ("bindcraft", "RMSD"): {
        "good": 1.5,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "Refolding RMSD against the designed backbone. Below 1.5 "
            "angstroms is good agreement; below 1.0 is excellent."
        ),
    },
    ("bindcraft", "shape_complementarity"): {
        "good": 0.65,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Shape complementarity at the interface. Above 0.65 is "
            "antibody-grade fit; above 0.75 is excellent."
        ),
    },
    ("bindcraft", "SAP"): {
        "good": 10,
        "excellent": 5,
        "direction": "lower_is_better",
        "explanation": (
            "Spatial Aggregation Propensity. Below 10 is acceptable; "
            "below 5 is favourable for biomanufacturing."
        ),
    },

    # ── RFantibody (antibody binder design) ──────────────────────────
    ("rfantibody", "ipAE"): {
        "good": 10.0,
        "excellent": 6.0,
        "direction": "lower_is_better",
        "explanation": (
            "Interaction pAE (binder-target). Below 10 angstroms is "
            "the RFantibody pass bar; below 6 is strong."
        ),
    },
    ("rfantibody", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "Predicted local confidence on the designed antibody. "
            "Above 80 is confidently folded; above 90 is high confidence."
        ),
    },
    ("rfantibody", "pAE"): {
        "good": 5.0,
        "excellent": 3.0,
        "direction": "lower_is_better",
        "explanation": (
            "Global predicted aligned error. Below 5 angstroms is good "
            "complex geometry; below 3 is excellent."
        ),
    },

    # ── PXDesign (backbone hallucination + AF2-IG re-score) ──────────
    ("pxdesign", "ipTM"): {
        "good": 0.75,
        "excellent": 0.85,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from AF2-IG re-scoring. Above 0.75 is a "
            "credible binder; above 0.85 is strong."
        ),
    },
    ("pxdesign", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence on the designed binder. Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    ("pxdesign", "pAE"): {
        "good": 5.0,
        "excellent": 3.0,
        "direction": "lower_is_better",
        "explanation": (
            "Global predicted aligned error. Below 5 angstroms is good "
            "complex geometry; below 3 is excellent."
        ),
    },

    # ── BoltzGen (Boltz-1 distilled generator + refold check) ────────
    ("boltzgen", "ipTM"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from the BoltzGen confidence head. Above "
            "0.7 is a credible binder; above 0.8 is strong."
        ),
    },
    ("boltzgen", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT-equivalent confidence on the generated structure. "
            "Above 80 is confidently folded; above 90 is high confidence."
        ),
    },
    ("boltzgen", "refolding_rmsd"): {
        "good": 1.5,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "Cross-check RMSD between the generator's structure and "
            "the AF2 refold of its sequence. Below 1.5 angstroms is "
            "self-consistent; below 1.0 is excellent."
        ),
    },

    # ── Boltz-2 (calibrated cofold validator) ────────────────────────
    ("boltz2", "ipTM"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from Boltz-2's calibrated confidence head. "
            "Above 0.7 is a credible binder; above 0.8 is strong."
        ),
    },
    ("boltz2", "pTM"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "pTM is global complex confidence. Above 0.7 is a credible "
            "model; above 0.8 is strong."
        ),
    },
    ("boltz2", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "Complex pLDDT (rescaled to the AF2 0-100 range). Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    ("boltz2", "n_hotspot_contacts"): {
        "good": 4,
        "excellent": 5,
        "direction": "higher_is_better",
        "explanation": (
            "Number of user-requested hotspots contacted by the binder. "
            "Above 4 of 7 is the strict-pass bar; 5 or more is strong."
        ),
    },
    ("iggm", "epitope_contacts"): {
        "good": 3,
        "excellent": 5,
        "direction": "higher_is_better",
        "explanation": (
            "Number of your requested epitope residues the designed "
            "antibody contacts. More engagement means the antibody is "
            "docking where you asked."
        ),
    },
}


def get_legend(tool_slug: str, column_key: str) -> Optional[Legend]:
    """Return the legend for ``(tool_slug, column_key)`` or None."""
    if not tool_slug or not column_key:
        return None
    return SCORE_LEGENDS.get((tool_slug, column_key))


def score_legends_for(tool_slug: str) -> dict[str, Legend]:
    """Return ``{column_key: Legend}`` for one tool. Empty dict on miss.

    Templates call this via the Jinja global registered in ``app.py``;
    the dict shape lets a template do ``legends.get(col)`` without ever
    importing the module.
    """
    if not tool_slug:
        return {}
    return {
        col: legend
        for (ts, col), legend in SCORE_LEGENDS.items()
        if ts == tool_slug
    }
