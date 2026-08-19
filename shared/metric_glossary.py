"""Single source of truth for metric definitions shown in the candidate table.

Each entry maps a score key (as it appears in ``job.result.candidates[].scores``)
to a display label, one-sentence definition, the "good" range, and the primary
citation. Referenced by the ``candidate_table.html`` macro and the export routes.
"""

from __future__ import annotations


GLOSSARY: dict[str, dict] = {
    "ranking_score": {
        "label": "Ranking score",
        "definition": (
            "OpenDDE's confidence-head ranking of a predicted complex. Used by the "
            "model to select the best sample; higher is more confident."
        ),
        "good_range": "higher is better (relative within a run)",
        "citation": "Aureka AI Research, OpenDDE-Preview 2026",
    },
    "ipTM": {
        "label": "ipTM",
        # DO NOT PROMISE WHICH INTERFACE. This entry is global — it is stacked
        # after the per-tool legend into ONE tooltip string
        # (components/candidate_table.html), and it used to end "at the
        # binder–target interface SPECIFICALLY". Four words after the boltzgen
        # legend says "an older run stored a complex-wide value instead", that
        # is a flat contradiction inside a single tooltip, and it is equally
        # false for rfdiffusion and pxdesign on a multi-chain target, where the
        # number covers the target's own chain-chain contact as well.
        #
        # What is true of ipTM everywhere is the CONSTRUCTION: a pTM restricted
        # to an interface rather than to the whole fold. Which chains that
        # interface spans is a per-tool fact, so it is left to the per-tool
        # legend (shared/score_legends.py) and to the multi-chain banner, both
        # of which know the tool and can say so without lying to the others.
        "definition": (
            "Interface predicted Template Modeling score. A pTM restricted to "
            "an interface rather than to the whole fold (0–1 scale). Which "
            "chains that interface spans depends on the tool that produced "
            "the number, and for some tools on when the run happened."
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
    "ipAE": {
        "label": "ipAE (Å)",
        "definition": (
            "Interaction Predicted Aligned Error. The pAE restricted to residue "
            "pairs that span the binder-target interface. RFantibody's primary "
            "binding-quality metric — a confident interface predicts a real "
            "interaction. Lower is better."
        ),
        "good_range": "< 10 Å passes; < 6 Å strong",
        "citation": "Bennett et al., bioRxiv 2024 (RFantibody)",
    },
    "i_pAE": {
        "label": "i_pAE (Å)",
        "definition": (
            "Interface Predicted Aligned Error. The pAE restricted to residue "
            "pairs that span the binder-target interface. AF2-multimer's "
            "binder-quality metric — a confident interface predicts a real "
            "interaction. Lower is better."
        ),
        "good_range": "< 10 Å passes; < 6 Å strong",
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
            "Pipeline quality gate result. 'pass' means the design cleared all "
            "production thresholds (AF2-IG re-scoring, RMSD, SC). 'below "
            "threshold' means the pipeline ran cleanly but the design did "
            "not meet pilot-tier quality bars — useful for inspecting the "
            "score distribution, not for advancing to validation. 'stub' "
            "marks smoke-test stubs whose scores are placeholders. "
            "'strict_pass' / 'soft_pass' are Boltz-2 cofold tiers: "
            "strict_pass = complex_pLDDT > 0.85 AND ipTM > 0.7 AND at "
            "least 5 hotspot contacts."
        ),
        "good_range": "pass / strict_pass",
        "citation": "",
    },
    "n_hotspot_contacts": {
        "label": "Hotspot hits",
        "definition": (
            "Number of user-requested antigen hotspot residues that the "
            "binder contacts in the predicted complex (any heavy atom "
            "within 5 Å). Boltz-2 cofold validation only."
        ),
        "good_range": "> 4 out of 7 typical for strict-pass designs",
        "citation": "",
    },
    "epitope_contacts": {
        "label": "Epitope contacts",
        "definition": (
            "Number of the epitope residues you requested that the "
            "designed antibody contacts in the predicted complex (any "
            "heavy atom within 5 Å). IgGM antibody design only."
        ),
        "good_range": "higher is better; more epitope engagement",
        "citation": "",
    },
    # Proteina's declared primary metric (shared/result_columns.py). Added for
    # the combined target table, whose Score cell prints the primary metric's
    # LABEL beside its value, so a metric with no glossary entry would render
    # its raw key.
    #
    # The definition names both variants deliberately. This is NOT one quantity:
    # tools/proteina/run_pipeline.py:116-117, verified there against the P-2 and
    # P-3 canary reward CSVs, records that the protein_binder reward comes from
    # the AF2 refold and equals -i_pAE, while the ligand_binder reward comes
    # from the RF3 fold. Describing it as a single score would be wrong, and it
    # is why shared/ranking.py keys its cohorts on (tool, preset) rather than on
    # tool alone: two proteina runs at different presets must never be ranked
    # against each other.
    # ADDED BECAUSE A TEMPLATE WAS STATING A THRESHOLD THIS FILE DID NOT HOLD.
    # components/about_panel.html renders a general "what good looks like"
    # legend on all 14 tool pages, and its recovery entry read "well
    # calibrated above roughly 0.4 on diverse folds" — a number with no
    # source, four entries below the ipTM one that had just been converted to
    # a glossary read. The number itself is right and was already sourced
    # elsewhere: shared/score_legends.py ("mpnn", "recovery") sets good=0.4
    # and excellent=0.6, and words them "Above 0.4 is a usable design; above
    # 0.6 is excellent". ``good_range`` below says that and nothing more —
    # "well calibrated ... on diverse folds" was an unsourced embellishment on
    # top of a sourced number.
    "recovery": {
        "label": "Sequence recovery",
        "definition": (
            "Fraction of the native residues a sequence-design model puts "
            "back when it redesigns a known sequence onto its own backbone. "
            "Higher means the model reproduces what nature chose more often."
        ),
        "good_range": "> 0.4 usable; > 0.6 excellent",
        "citation": "Dauparas et al., Science 2022 (ProteinMPNN)",
    },
    "total_reward": {
        "label": "Reward",
        "definition": (
            "Proteina's composite design reward. What it measures depends on "
            "the preset: for protein binders it is the negated AF2 interface "
            "pAE (so a value of -6 means an i_pAE of 6 Å), and for ligand "
            "binders it is derived from the RF3 fold instead. Comparable "
            "within one preset, not across two."
        ),
        "good_range": "higher is better, within a single preset",
        "citation": "",
    },
}

# Display format per metric (Python format spec applied to the float value).
# "str" means no numeric conversion — render as-is.
_FORMAT: dict[str, str] = {
    "ipTM": ".3f",
    "pLDDT": ".1f",
    "pAE": ".2f",
    "ipAE": ".2f",
    "i_pAE": ".2f",
    "pTM": ".3f",
    "refolding_rmsd": ".2f",
    "RMSD": ".2f",
    "shape_complementarity": ".3f",
    "SAP": ".2f",
    "filter_status": "str",
    "n_hotspot_contacts": ".0f",
    "epitope_contacts": ".0f",
    # A 0-1 fraction. The ".3f" default printed a third digit ProteinMPNN's
    # own FASTA header does not carry.
    "recovery": ".2f",
    # Two decimals, matching ipAE: under the protein_binder preset this IS an
    # interface pAE in Angstrom, negated. Without an entry it fell to the ".3f"
    # default and printed a third digit the underlying number does not carry.
    "total_reward": ".2f",
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
