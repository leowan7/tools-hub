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
            "Interface pTM for the binder-to-target interface "
            "(design_iptm) from the BoltzGen confidence head. Above "
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


# ---------------------------------------------------------------------------
# Multi-chain ipTM is not comparable
# ---------------------------------------------------------------------------
#
# ipTM is a MAX over residues, not a mean. On a single-chain target that is
# harmless — the only interface in the complex is the one you designed. On a
# multi-chain target it is not: a real crystal dimer's own chain-chain
# interface scores ~0.9 and dominates the number almost independently of
# binder quality, so a mediocre binder can rank first carrying a
# plausible-looking score. docs/MULTI-CHAIN-TARGETS.md states it precisely.
#
# This matters more than a mis-rendered number because ipTM is also the
# RANKING key (shared/result_columns.py) and the threshold that labels
# filter_status.
#
# BoltzGen is fixed at the source: llm-proteinDesigner PR #18 moves
# `design_iptm` — the real binder-to-target pair — to the front of IPTM_KEYS.
# It stays in this set until that PR is MERGED AND DEPLOYED, because until
# then the deployed container still reports the complex-wide value. Removing
# it is a one-line follow-up; cite the deploy in that commit.
#
# rfdiffusion and pxdesign have no equivalent fix available: the per-pair
# value does not exist anywhere in their output and deriving it from the chain
# layout is a separate piece of work. For them this notice is the remedy, not
# a stopgap.
#
# bindcraft is included even though multi_chain_container_ready=False blocks
# the tool-form path, because the campaign and target-launch routes may not
# call preflight_for_tool at all (an open item in
# docs/HANDOFF-2026-08-07-multichain-finish.md). The notice is non-blocking,
# so a false positive costs a sentence and a false negative costs trust in a
# number.
#
# PROTEINA IS DELIBERATELY ABSENT, and it is the exclusion worth arguing,
# because proteina is the only tool that has actually run a multi-chain target
# on a GPU here. It does surface an interface score — ``af2_iptm``, resolved
# from ``rf3folding_ipTM`` first (tools/proteina/run_pipeline.py) — and that
# number is open to the same inflation as everyone else's. But it is NOT what
# proteina ranks on: its primary metric is ``total_reward``
# (shared/result_columns.py), which proteina defines as ``-i_pae``, an
# interface PAE. So the second half of this notice — "these designs are also
# ranked by it", the half that makes it worth interrupting the user for —
# would be FALSE on a proteina table. A caveat that overstates its own scope
# is precisely the failure this notice exists to avoid.
#
# Open, and deliberately NOT resolved here: proteina's ``af2_iptm`` COLUMN may
# still read high on a multi-chain target for the reasons above, and whether
# i_pae is itself computed over the right chain pair has not been traced. That
# needs its own copy and its own verification, not membership in this set.
# Filed, not fixed.
MULTICHAIN_IPTM_UNRELIABLE_TOOLS = frozenset(
    {"rfdiffusion", "pxdesign", "bindcraft", "boltzgen"}
)


def multichain_iptm_unreliable(tools, target_chain: str) -> bool:
    """Should the results view warn that ipTM cannot be read at face value?

    True only when the target names more than one chain AND at least one tool
    in play ranks on a complex-wide ipTM. Registered as a Jinja global in
    ``app.py`` so the decision stays here, in Python, where it is testable —
    rather than as a string-splitting expression repeated across six templates.

    ``tools`` is a single slug on a job or campaign page, or an iterable of
    slugs on the pooled target page, where one table mixes several tools and
    the caveat applies if ANY of them is affected.

    Both chain separators are accepted, matching every other consumer of this
    field (``"A,B"`` and ``"A B"``); see ``tools.base.parse_target_chains``.
    """
    chains = [c for c in str(target_chain or "").replace(",", " ").split() if c]
    if len(set(chains)) <= 1:
        return False
    if not tools:
        return False
    if isinstance(tools, str):
        tools = [tools]
    return any(t in MULTICHAIN_IPTM_UNRELIABLE_TOOLS for t in tools)
